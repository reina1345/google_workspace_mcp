"""
Google Drive MCP Tools

This module provides MCP tools for interacting with Google Drive API.
"""

import asyncio
import logging
import io
import httpx
import base64

from typing import Optional, List, Dict, Any
from tempfile import NamedTemporaryFile
from urllib.parse import urlparse
from urllib.request import url2pathname
from pathlib import Path

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from auth.service_decorator import require_google_service
from auth.oauth_config import is_stateless_mode
from core.attachment_storage import get_attachment_storage, get_attachment_url
from core.utils import extract_office_xml_text, handle_http_errors
from core.server import server
from core.config import get_transport_mode
from gdrive.drive_helpers import (
    DRIVE_QUERY_PATTERNS,
    build_drive_list_params,
    check_public_link_permission,
    format_permission_info,
    get_drive_image_url,
    resolve_drive_item,
    resolve_folder_id,
    validate_expiration_time,
    validate_share_role,
    validate_share_type,
)

logger = logging.getLogger(__name__)

DOWNLOAD_CHUNK_SIZE_BYTES = 256 * 1024  # 256 KB
UPLOAD_CHUNK_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB (Google recommended minimum)


@server.tool()
@handle_http_errors("search_drive_files", is_read_only=True, service_type="drive")
@require_google_service("drive", "drive_read")
async def search_drive_files(
    service,
    user_google_email: str,
    query: str,
    page_size: int = 10,
    drive_id: Optional[str] = None,
    include_items_from_all_drives: bool = True,
    corpora: Optional[str] = None,
) -> str:
    """
    ユーザーのGoogleドライブ（共有ドライブを含む）内のファイルやフォルダを検索します。

    Args:
        user_google_email (str): ユーザーのGoogleメールアドレス。必須。
        query (str): 検索クエリ文字列。Google Drive検索演算子をサポートします。
        page_size (int): 返されるファイルの最大数。デフォルトは10です。
        drive_id (Optional[str]): 検索する共有ドライブのID。Noneの場合、動作は `corpora` と `include_items_from_all_drives` に依存します。
        include_items_from_all_drives (bool): 共有ドライブのアイテムを結果に含めるかどうか。デフォルトはTrueです。 `drive_id` を指定しない場合に有効です。
        corpora (Optional[str]): クエリするアイテムの範囲（例: 'user', 'domain', 'drive', 'allDrives'）。
                                 'drive_id' が指定され、'corpora' がNoneの場合、デフォルトで 'drive' になります。
                                 それ以外の場合はDrive APIのデフォルト動作が適用されます。効率性のために 'allDrives' よりも 'user' や 'drive' を推奨します。

    Returns:
        str: 見つかったファイル/フォルダのフォーマット済みリスト（ID、名前、タイプ、サイズ、更新日時、リンク）。
    """
    logger.info(
        f"[search_drive_files] Invoked. Email: '{user_google_email}', Query: '{query}'"
    )

    # Check if the query looks like a structured Drive query or free text
    # Look for Drive API operators and structured query patterns
    is_structured_query = any(pattern.search(query) for pattern in DRIVE_QUERY_PATTERNS)

    if is_structured_query:
        final_query = query
        logger.info(
            f"[search_drive_files] Using structured query as-is: '{final_query}'"
        )
    else:
        # For free text queries, wrap in fullText contains
        escaped_query = query.replace("'", "\\'")
        final_query = f"fullText contains '{escaped_query}'"
        logger.info(
            f"[search_drive_files] Reformatting free text query '{query}' to '{final_query}'"
        )

    list_params = build_drive_list_params(
        query=final_query,
        page_size=page_size,
        drive_id=drive_id,
        include_items_from_all_drives=include_items_from_all_drives,
        corpora=corpora,
    )

    results = await asyncio.to_thread(service.files().list(**list_params).execute)
    files = results.get("files", [])
    if not files:
        return f"No files found for '{query}'."

    formatted_files_text_parts = [
        f"Found {len(files)} files for {user_google_email} matching '{query}':"
    ]
    for item in files:
        size_str = f", Size: {item.get('size', 'N/A')}" if "size" in item else ""
        formatted_files_text_parts.append(
            f'- Name: "{item["name"]}" (ID: {item["id"]}, Type: {item["mimeType"]}{size_str}, Modified: {item.get("modifiedTime", "N/A")}) Link: {item.get("webViewLink", "#")}'
        )
    text_output = "\n".join(formatted_files_text_parts)
    return text_output


@server.tool()
@handle_http_errors("get_drive_file_content", is_read_only=True, service_type="drive")
@require_google_service("drive", "drive_read")
async def get_drive_file_content(
    service,
    user_google_email: str,
    file_id: str,
) -> str:
    """
    指定されたGoogleドライブファイルのコンテンツをIDで取得し、共有ドライブ内のファイルもサポートします。

    • ネイティブGoogleドキュメント、スプレッドシート、スライド → テキスト / CSVとしてエクスポートされます。
    • Officeファイル (.docx, .xlsx, .pptx) → 解凍され、標準ライブラリを使用して解析され、
      読み取り可能なテキストが抽出されます。
    • その他のファイル → ダウンロードされます。UTF-8デコードを試み、失敗した場合はバイナリであることを通知します。

    Args:
        user_google_email: ユーザーのGoogleメールアドレス。
        file_id: ドライブファイルID。

    Returns:
        str: メタデータヘッダー付きのプレーンテキストとしてのファイルコンテンツ。
    """
    logger.info(f"[get_drive_file_content] Invoked. File ID: '{file_id}'")

    resolved_file_id, file_metadata = await resolve_drive_item(
        service,
        file_id,
        extra_fields="name, webViewLink",
    )
    file_id = resolved_file_id
    mime_type = file_metadata.get("mimeType", "")
    file_name = file_metadata.get("name", "Unknown File")
    export_mime_type = {
        "application/vnd.google-apps.document": "text/plain",
        "application/vnd.google-apps.spreadsheet": "text/csv",
        "application/vnd.google-apps.presentation": "text/plain",
    }.get(mime_type)

    request_obj = (
        service.files().export_media(fileId=file_id, mimeType=export_mime_type)
        if export_mime_type
        else service.files().get_media(fileId=file_id)
    )
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request_obj)
    loop = asyncio.get_event_loop()
    done = False
    while not done:
        status, done = await loop.run_in_executor(None, downloader.next_chunk)

    file_content_bytes = fh.getvalue()

    # Attempt Office XML extraction only for actual Office XML files
    office_mime_types = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }

    if mime_type in office_mime_types:
        office_text = extract_office_xml_text(file_content_bytes, mime_type)
        if office_text:
            body_text = office_text
        else:
            # Fallback: try UTF-8; otherwise flag binary
            try:
                body_text = file_content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                body_text = (
                    f"[Binary or unsupported text encoding for mimeType '{mime_type}' - "
                    f"{len(file_content_bytes)} bytes]"
                )
    else:
        # For non-Office files (including Google native files), try UTF-8 decode directly
        try:
            body_text = file_content_bytes.decode("utf-8")
        except UnicodeDecodeError:
            body_text = (
                f"[Binary or unsupported text encoding for mimeType '{mime_type}' - "
                f"{len(file_content_bytes)} bytes]"
            )

    # Assemble response
    header = (
        f'File: "{file_name}" (ID: {file_id}, Type: {mime_type})\n'
        f"Link: {file_metadata.get('webViewLink', '#')}\n\n--- CONTENT ---\n"
    )
    return header + body_text


@server.tool()
@handle_http_errors(
    "get_drive_file_download_url", is_read_only=True, service_type="drive"
)
@require_google_service("drive", "drive_read")
async def get_drive_file_download_url(
    service,
    user_google_email: str,
    file_id: str,
    export_format: Optional[str] = None,
) -> str:
    """
    GoogleドライブファイルのダウンロードURLを取得します。ファイルは準備され、HTTP URL経由で利用可能になります。

    Googleネイティブファイル（Docs, Sheets, Slides）の場合、有用な形式にエクスポートします：
    • Google Docs → PDF（デフォルト）または export_format='docx' の場合はDOCX
    • Google Sheets → XLSX（デフォルト）または export_format='csv' の場合はCSV
    • Google Slides → PDF（デフォルト）または export_format='pptx' の場合はPPTX

    その他のファイルの場合、元のファイル形式をダウンロードします。

    Args:
        user_google_email: ユーザーのGoogleメールアドレス。必須。
        file_id: ダウンロードURLを取得するGoogleドライブファイルID。
        export_format: Googleネイティブファイルのオプションのエクスポート形式。
                      オプション: 'pdf', 'docx', 'xlsx', 'csv', 'pptx'。
                      指定されない場合、適切なデフォルト（Docs/SlidesはPDF、SheetsはXLSX）を使用します。

    Returns:
        str: ダウンロードURLとファイルメタデータ。ファイルはURLで1時間利用可能です。
    """
    logger.info(
        f"[get_drive_file_download_url] Invoked. File ID: '{file_id}', Export format: {export_format}"
    )

    # Resolve shortcuts and get file metadata
    resolved_file_id, file_metadata = await resolve_drive_item(
        service,
        file_id,
        extra_fields="name, webViewLink, mimeType",
    )
    file_id = resolved_file_id
    mime_type = file_metadata.get("mimeType", "")
    file_name = file_metadata.get("name", "Unknown File")

    # Determine export format for Google native files
    export_mime_type = None
    output_filename = file_name
    output_mime_type = mime_type

    if mime_type == "application/vnd.google-apps.document":
        # Google Docs
        if export_format == "docx":
            export_mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            output_mime_type = export_mime_type
            if not output_filename.endswith(".docx"):
                output_filename = f"{Path(output_filename).stem}.docx"
        else:
            # Default to PDF
            export_mime_type = "application/pdf"
            output_mime_type = export_mime_type
            if not output_filename.endswith(".pdf"):
                output_filename = f"{Path(output_filename).stem}.pdf"

    elif mime_type == "application/vnd.google-apps.spreadsheet":
        # Google Sheets
        if export_format == "csv":
            export_mime_type = "text/csv"
            output_mime_type = export_mime_type
            if not output_filename.endswith(".csv"):
                output_filename = f"{Path(output_filename).stem}.csv"
        else:
            # Default to XLSX
            export_mime_type = (
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            output_mime_type = export_mime_type
            if not output_filename.endswith(".xlsx"):
                output_filename = f"{Path(output_filename).stem}.xlsx"

    elif mime_type == "application/vnd.google-apps.presentation":
        # Google Slides
        if export_format == "pptx":
            export_mime_type = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            output_mime_type = export_mime_type
            if not output_filename.endswith(".pptx"):
                output_filename = f"{Path(output_filename).stem}.pptx"
        else:
            # Default to PDF
            export_mime_type = "application/pdf"
            output_mime_type = export_mime_type
            if not output_filename.endswith(".pdf"):
                output_filename = f"{Path(output_filename).stem}.pdf"

    # Download the file
    request_obj = (
        service.files().export_media(fileId=file_id, mimeType=export_mime_type)
        if export_mime_type
        else service.files().get_media(fileId=file_id)
    )

    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request_obj)
    loop = asyncio.get_event_loop()
    done = False
    while not done:
        status, done = await loop.run_in_executor(None, downloader.next_chunk)

    file_content_bytes = fh.getvalue()
    size_bytes = len(file_content_bytes)
    size_kb = size_bytes / 1024 if size_bytes else 0

    # Check if we're in stateless mode (can't save files)
    if is_stateless_mode():
        result_lines = [
            "File downloaded successfully!",
            f"File: {file_name}",
            f"File ID: {file_id}",
            f"Size: {size_kb:.1f} KB ({size_bytes} bytes)",
            f"MIME Type: {output_mime_type}",
            "\n⚠️ Stateless mode: File storage disabled.",
            "\nBase64-encoded content (first 100 characters shown):",
            f"{base64.b64encode(file_content_bytes[:100]).decode('utf-8')}...",
        ]
        logger.info(
            f"[get_drive_file_download_url] Successfully downloaded {size_kb:.1f} KB file (stateless mode)"
        )
        return "\n".join(result_lines)

    # Save file and generate URL
    try:
        storage = get_attachment_storage()

        # Encode bytes to base64 (as expected by AttachmentStorage)
        base64_data = base64.urlsafe_b64encode(file_content_bytes).decode("utf-8")

        # Save attachment
        saved_file_id = storage.save_attachment(
            base64_data=base64_data,
            filename=output_filename,
            mime_type=output_mime_type,
        )

        # Generate URL
        download_url = get_attachment_url(saved_file_id)

        result_lines = [
            "File downloaded successfully!",
            f"File: {file_name}",
            f"File ID: {file_id}",
            f"Size: {size_kb:.1f} KB ({size_bytes} bytes)",
            f"MIME Type: {output_mime_type}",
            f"\n📎 Download URL: {download_url}",
            "\nThe file has been saved and is available at the URL above.",
            "The file will expire after 1 hour.",
        ]

        if export_mime_type:
            result_lines.append(
                f"\nNote: Google native file exported to {output_mime_type} format."
            )

        logger.info(
            f"[get_drive_file_download_url] Successfully saved {size_kb:.1f} KB file as {saved_file_id}"
        )
        return "\n".join(result_lines)

    except Exception as e:
        logger.error(f"[get_drive_file_download_url] Failed to save file: {e}")
        return (
            f"Error: Failed to save file for download.\n"
            f"File was downloaded successfully ({size_kb:.1f} KB) but could not be saved.\n\n"
            f"Error details: {str(e)}"
        )


@server.tool()
@handle_http_errors("list_drive_items", is_read_only=True, service_type="drive")
@require_google_service("drive", "drive_read")
async def list_drive_items(
    service,
    user_google_email: str,
    folder_id: str = "root",
    page_size: int = 100,
    drive_id: Optional[str] = None,
    include_items_from_all_drives: bool = True,
    corpora: Optional[str] = None,
) -> str:
    """
    ファイルとフォルダを一覧表示し、共有ドライブもサポートします。
    `drive_id` が指定されている場合、その共有ドライブ内のアイテムを一覧表示します。`folder_id` はそのドライブに対する相対パスとなります（またはルートとして drive_id を folder_id として使用します）。
    `drive_id` が指定されていない場合、ユーザーの「マイドライブ」およびアクセス可能な共有ドライブ（`include_items_from_all_drives` が True の場合）からアイテムを一覧表示します。

    Args:
        user_google_email (str): ユーザーのGoogleメールアドレス。必須。
        folder_id (str): GoogleドライブフォルダのID。デフォルトは 'root' です。共有ドライブの場合、これは共有ドライブのID（ルートを一覧表示する場合）か、その共有ドライブ内のフォルダIDになります。
        page_size (int): 返されるアイテムの最大数。デフォルトは100です。
        drive_id (Optional[str]): 共有ドライブのID。指定された場合、一覧はこのドライブにスコープされます。
        include_items_from_all_drives (bool): `drive_id` が設定されていない場合、アクセス可能なすべての共有ドライブのアイテムを含めるかどうか。デフォルトはTrueです。
        corpora (Optional[str]): クエリするコーパス（'user', 'drive', 'allDrives'）。`drive_id` が設定され、`corpora` がNoneの場合、'drive' が使用されます。Noneでかつ `drive_id` もない場合はAPIのデフォルトが適用されます。

    Returns:
        str: 指定されたフォルダ内のファイル/フォルダのフォーマット済みリスト。
    """
    logger.info(
        f"[list_drive_items] Invoked. Email: '{user_google_email}', Folder ID: '{folder_id}'"
    )

    resolved_folder_id = await resolve_folder_id(service, folder_id)
    final_query = f"'{resolved_folder_id}' in parents and trashed=false"

    list_params = build_drive_list_params(
        query=final_query,
        page_size=page_size,
        drive_id=drive_id,
        include_items_from_all_drives=include_items_from_all_drives,
        corpora=corpora,
    )

    results = await asyncio.to_thread(service.files().list(**list_params).execute)
    files = results.get("files", [])
    if not files:
        return f"No items found in folder '{folder_id}'."

    formatted_items_text_parts = [
        f"Found {len(files)} items in folder '{folder_id}' for {user_google_email}:"
    ]
    for item in files:
        size_str = f", Size: {item.get('size', 'N/A')}" if "size" in item else ""
        formatted_items_text_parts.append(
            f'- Name: "{item["name"]}" (ID: {item["id"]}, Type: {item["mimeType"]}{size_str}, Modified: {item.get("modifiedTime", "N/A")}) Link: {item.get("webViewLink", "#")}'
        )
    text_output = "\n".join(formatted_items_text_parts)
    return text_output


@server.tool()
@handle_http_errors("create_drive_file", service_type="drive")
@require_google_service("drive", "drive_file")
async def create_drive_file(
    service,
    user_google_email: str,
    file_name: str,
    content: Optional[str] = None,  # Now explicitly Optional
    folder_id: str = "root",
    mime_type: str = "text/plain",
    fileUrl: Optional[str] = None,  # Now explicitly Optional
) -> str:
    """
    Googleドライブに新しいファイルを作成し、共有ドライブ内での作成もサポートします。
    直接的なコンテンツ、またはコンテンツを取得するためのfileUrlのいずれかを受け入れます。

    Args:
        user_google_email (str): ユーザーのGoogleメールアドレス。必須。
        file_name (str): 新しいファイルの名前。
        content (Optional[str]): 指定された場合、ファイルに書き込むコンテンツ。
        folder_id (str): 親フォルダのID。デフォルトは 'root' です。共有ドライブの場合、共有ドライブ内のフォルダIDである必要があります。
        mime_type (str): ファイルのMIMEタイプ。デフォルトは 'text/plain' です。
        fileUrl (Optional[str]): 指定された場合、このURLからファイルコンテンツを取得します。file://, http://, https:// プロトコルをサポートします。

    Returns:
        str: ファイルリンクを含む、ファイル作成成功の確認メッセージ。
    """
    logger.info(
        f"[create_drive_file] Invoked. Email: '{user_google_email}', File Name: {file_name}, Folder ID: {folder_id}, fileUrl: {fileUrl}"
    )

    if not content and not fileUrl:
        raise Exception("You must provide either 'content' or 'fileUrl'.")

    file_data = None
    resolved_folder_id = await resolve_folder_id(service, folder_id)

    file_metadata = {
        "name": file_name,
        "parents": [resolved_folder_id],
        "mimeType": mime_type,
    }

    # Prefer fileUrl if both are provided
    if fileUrl:
        logger.info(f"[create_drive_file] Fetching file from URL: {fileUrl}")

        # Check if this is a file:// URL
        parsed_url = urlparse(fileUrl)
        if parsed_url.scheme == "file":
            # Handle file:// URL - read from local filesystem
            logger.info(
                "[create_drive_file] Detected file:// URL, reading from local filesystem"
            )
            transport_mode = get_transport_mode()
            running_streamable = transport_mode == "streamable-http"
            if running_streamable:
                logger.warning(
                    "[create_drive_file] file:// URL requested while server runs in streamable-http mode. Ensure the file path is accessible to the server (e.g., Docker volume) or use an HTTP(S) URL."
                )

            # Convert file:// URL to a cross-platform local path
            raw_path = parsed_url.path or ""
            netloc = parsed_url.netloc
            if netloc and netloc.lower() != "localhost":
                raw_path = f"//{netloc}{raw_path}"
            file_path = url2pathname(raw_path)

            # Verify file exists
            path_obj = Path(file_path)
            if not path_obj.exists():
                extra = (
                    " The server is running via streamable-http, so file:// URLs must point to files inside the container or remote host."
                    if running_streamable
                    else ""
                )
                raise Exception(f"Local file does not exist: {file_path}.{extra}")
            if not path_obj.is_file():
                extra = (
                    " In streamable-http/Docker deployments, mount the file into the container or provide an HTTP(S) URL."
                    if running_streamable
                    else ""
                )
                raise Exception(f"Path is not a file: {file_path}.{extra}")

            logger.info(f"[create_drive_file] Reading local file: {file_path}")

            # Read file and upload
            file_data = await asyncio.to_thread(path_obj.read_bytes)
            total_bytes = len(file_data)
            logger.info(f"[create_drive_file] Read {total_bytes} bytes from local file")

            media = MediaIoBaseUpload(
                io.BytesIO(file_data),
                mimetype=mime_type,
                resumable=True,
                chunksize=UPLOAD_CHUNK_SIZE_BYTES,
            )

            logger.info("[create_drive_file] Starting upload to Google Drive...")
            created_file = await asyncio.to_thread(
                service.files()
                .create(
                    body=file_metadata,
                    media_body=media,
                    fields="id, name, webViewLink",
                    supportsAllDrives=True,
                )
                .execute
            )
        # Handle HTTP/HTTPS URLs
        elif parsed_url.scheme in ("http", "https"):
            # when running in stateless mode, deployment may not have access to local file system
            if is_stateless_mode():
                async with httpx.AsyncClient(follow_redirects=True) as client:
                    resp = await client.get(fileUrl)
                    if resp.status_code != 200:
                        raise Exception(
                            f"Failed to fetch file from URL: {fileUrl} (status {resp.status_code})"
                        )
                    file_data = await resp.aread()
                    # Try to get MIME type from Content-Type header
                    content_type = resp.headers.get("Content-Type")
                    if content_type and content_type != "application/octet-stream":
                        mime_type = content_type
                        file_metadata["mimeType"] = content_type
                        logger.info(
                            f"[create_drive_file] Using MIME type from Content-Type header: {content_type}"
                        )

                media = MediaIoBaseUpload(
                    io.BytesIO(file_data),
                    mimetype=mime_type,
                    resumable=True,
                    chunksize=UPLOAD_CHUNK_SIZE_BYTES,
                )

                created_file = await asyncio.to_thread(
                    service.files()
                    .create(
                        body=file_metadata,
                        media_body=media,
                        fields="id, name, webViewLink",
                        supportsAllDrives=True,
                    )
                    .execute
                )
            else:
                # Use NamedTemporaryFile to stream download and upload
                with NamedTemporaryFile() as temp_file:
                    total_bytes = 0
                    # follow redirects
                    async with httpx.AsyncClient(follow_redirects=True) as client:
                        async with client.stream("GET", fileUrl) as resp:
                            if resp.status_code != 200:
                                raise Exception(
                                    f"Failed to fetch file from URL: {fileUrl} (status {resp.status_code})"
                                )

                            # Stream download in chunks
                            async for chunk in resp.aiter_bytes(
                                chunk_size=DOWNLOAD_CHUNK_SIZE_BYTES
                            ):
                                await asyncio.to_thread(temp_file.write, chunk)
                                total_bytes += len(chunk)

                            logger.info(
                                f"[create_drive_file] Downloaded {total_bytes} bytes from URL before upload."
                            )

                            # Try to get MIME type from Content-Type header
                            content_type = resp.headers.get("Content-Type")
                            if (
                                content_type
                                and content_type != "application/octet-stream"
                            ):
                                mime_type = content_type
                                file_metadata["mimeType"] = mime_type
                                logger.info(
                                    f"[create_drive_file] Using MIME type from Content-Type header: {mime_type}"
                                )

                    # Reset file pointer to beginning for upload
                    temp_file.seek(0)

                    # Upload with chunking
                    media = MediaIoBaseUpload(
                        temp_file,
                        mimetype=mime_type,
                        resumable=True,
                        chunksize=UPLOAD_CHUNK_SIZE_BYTES,
                    )

                    logger.info(
                        "[create_drive_file] Starting upload to Google Drive..."
                    )
                    created_file = await asyncio.to_thread(
                        service.files()
                        .create(
                            body=file_metadata,
                            media_body=media,
                            fields="id, name, webViewLink",
                            supportsAllDrives=True,
                        )
                        .execute
                    )
        else:
            if not parsed_url.scheme:
                raise Exception(
                    "fileUrl is missing a URL scheme. Use file://, http://, or https://."
                )
            raise Exception(
                f"Unsupported URL scheme '{parsed_url.scheme}'. Only file://, http://, and https:// are supported."
            )
    elif content:
        file_data = content.encode("utf-8")
        media = io.BytesIO(file_data)

        created_file = await asyncio.to_thread(
            service.files()
            .create(
                body=file_metadata,
                media_body=MediaIoBaseUpload(media, mimetype=mime_type, resumable=True),
                fields="id, name, webViewLink",
                supportsAllDrives=True,
            )
            .execute
        )

    link = created_file.get("webViewLink", "No link available")
    confirmation_message = f"Successfully created file '{created_file.get('name', file_name)}' (ID: {created_file.get('id', 'N/A')}) in folder '{folder_id}' for {user_google_email}. Link: {link}"
    logger.info(f"Successfully created file. Link: {link}")
    return confirmation_message


@server.tool()
@handle_http_errors(
    "get_drive_file_permissions", is_read_only=True, service_type="drive"
)
@require_google_service("drive", "drive_read")
async def get_drive_file_permissions(
    service,
    user_google_email: str,
    file_id: str,
) -> str:
    """
    共有権限を含むGoogleドライブファイルの詳細なメタデータを取得します。

    Args:
        user_google_email (str): ユーザーのGoogleメールアドレス。必須。
        file_id (str): 権限を確認するファイルのID。

    Returns:
        str: 共有ステータスとURLを含む詳細なファイルメタデータ。
    """
    logger.info(
        f"[get_drive_file_permissions] Checking file {file_id} for {user_google_email}"
    )

    resolved_file_id, _ = await resolve_drive_item(service, file_id)
    file_id = resolved_file_id

    try:
        # Get comprehensive file metadata including permissions with details
        file_metadata = await asyncio.to_thread(
            service.files()
            .get(
                fileId=file_id,
                fields="id, name, mimeType, size, modifiedTime, owners, "
                "permissions(id, type, role, emailAddress, domain, expirationTime, permissionDetails), "
                "webViewLink, webContentLink, shared, sharingUser, viewersCanCopyContent",
                supportsAllDrives=True,
            )
            .execute
        )

        # Format the response
        output_parts = [
            f"File: {file_metadata.get('name', 'Unknown')}",
            f"ID: {file_id}",
            f"Type: {file_metadata.get('mimeType', 'Unknown')}",
            f"Size: {file_metadata.get('size', 'N/A')} bytes",
            f"Modified: {file_metadata.get('modifiedTime', 'N/A')}",
            "",
            "Sharing Status:",
            f"  Shared: {file_metadata.get('shared', False)}",
        ]

        # Add sharing user if available
        sharing_user = file_metadata.get("sharingUser")
        if sharing_user:
            output_parts.append(
                f"  Shared by: {sharing_user.get('displayName', 'Unknown')} ({sharing_user.get('emailAddress', 'Unknown')})"
            )

        # Process permissions
        permissions = file_metadata.get("permissions", [])
        if permissions:
            output_parts.append(f"  Number of permissions: {len(permissions)}")
            output_parts.append("  Permissions:")
            for perm in permissions:
                output_parts.append(f"    - {format_permission_info(perm)}")
        else:
            output_parts.append("  No additional permissions (private file)")

        # Add URLs
        output_parts.extend(
            [
                "",
                "URLs:",
                f"  View Link: {file_metadata.get('webViewLink', 'N/A')}",
            ]
        )

        # webContentLink is only available for files that can be downloaded
        web_content_link = file_metadata.get("webContentLink")
        if web_content_link:
            output_parts.append(f"  Direct Download Link: {web_content_link}")

        has_public_link = check_public_link_permission(permissions)

        if has_public_link:
            output_parts.extend(
                [
                    "",
                    "✅ This file is shared with 'Anyone with the link' - it can be inserted into Google Docs",
                ]
            )
        else:
            output_parts.extend(
                [
                    "",
                    "❌ This file is NOT shared with 'Anyone with the link' - it cannot be inserted into Google Docs",
                    "   To fix: Right-click the file in Google Drive → Share → Anyone with the link → Viewer",
                ]
            )

        return "\n".join(output_parts)

    except Exception as e:
        logger.error(f"Error getting file permissions: {e}")
        return f"Error getting file permissions: {e}"


@server.tool()
@handle_http_errors(
    "check_drive_file_public_access", is_read_only=True, service_type="drive"
)
@require_google_service("drive", "drive_read")
async def check_drive_file_public_access(
    service,
    user_google_email: str,
    file_name: str,
) -> str:
    """
    ファイル名で検索し、公開リンク共有が有効になっているかを確認します。

    Args:
        user_google_email (str): ユーザーのGoogleメールアドレス。必須。
        file_name (str): 確認するファイルの名前。

    Returns:
        str: ファイルの共有ステータスと、Google Docsで使用可能かどうかに関する情報。
    """
    logger.info(f"[check_drive_file_public_access] Searching for {file_name}")

    # Search for the file
    escaped_name = file_name.replace("'", "\\'")
    query = f"name = '{escaped_name}'"

    list_params = {
        "q": query,
        "pageSize": 10,
        "fields": "files(id, name, mimeType, webViewLink)",
        "supportsAllDrives": True,
        "includeItemsFromAllDrives": True,
    }

    results = await asyncio.to_thread(service.files().list(**list_params).execute)

    files = results.get("files", [])
    if not files:
        return f"No file found with name '{file_name}'"

    if len(files) > 1:
        output_parts = [f"Found {len(files)} files with name '{file_name}':"]
        for f in files:
            output_parts.append(f"  - {f['name']} (ID: {f['id']})")
        output_parts.append("\nChecking the first file...")
        output_parts.append("")
    else:
        output_parts = []

    # Check permissions for the first file
    file_id = files[0]["id"]
    resolved_file_id, _ = await resolve_drive_item(service, file_id)
    file_id = resolved_file_id

    # Get detailed permissions
    file_metadata = await asyncio.to_thread(
        service.files()
        .get(
            fileId=file_id,
            fields="id, name, mimeType, permissions, webViewLink, webContentLink, shared",
            supportsAllDrives=True,
        )
        .execute
    )

    permissions = file_metadata.get("permissions", [])

    has_public_link = check_public_link_permission(permissions)

    output_parts.extend(
        [
            f"File: {file_metadata['name']}",
            f"ID: {file_id}",
            f"Type: {file_metadata['mimeType']}",
            f"Shared: {file_metadata.get('shared', False)}",
            "",
        ]
    )

    if has_public_link:
        output_parts.extend(
            [
                "✅ PUBLIC ACCESS ENABLED - This file can be inserted into Google Docs",
                f"Use with insert_doc_image_url: {get_drive_image_url(file_id)}",
            ]
        )
    else:
        output_parts.extend(
            [
                "❌ NO PUBLIC ACCESS - Cannot insert into Google Docs",
                "Fix: Drive → Share → 'Anyone with the link' → 'Viewer'",
            ]
        )

    return "\n".join(output_parts)


@server.tool()
@handle_http_errors("update_drive_file", is_read_only=False, service_type="drive")
@require_google_service("drive", "drive_file")
async def update_drive_file(
    service,
    user_google_email: str,
    file_id: str,
    # File metadata updates
    name: Optional[str] = None,
    description: Optional[str] = None,
    mime_type: Optional[str] = None,
    # Folder organization
    add_parents: Optional[str] = None,  # Comma-separated folder IDs to add
    remove_parents: Optional[str] = None,  # Comma-separated folder IDs to remove
    # File status
    starred: Optional[bool] = None,
    trashed: Optional[bool] = None,
    # Sharing and permissions
    writers_can_share: Optional[bool] = None,
    copy_requires_writer_permission: Optional[bool] = None,
    # Custom properties
    properties: Optional[dict] = None,  # User-visible custom properties
) -> str:
    """
    Googleドライブファイルのメタデータとプロパティを更新します。

    Args:
        user_google_email (str): ユーザーのGoogleメールアドレス。必須。
        file_id (str): 更新するファイルのID。必須。
        name (Optional[str]): ファイルの新しい名前。
        description (Optional[str]): ファイルの新しい説明。
        mime_type (Optional[str]): 新しいMIMEタイプ（注意: タイプの変更にはコンテンツのアップロードが必要な場合があります）。
        add_parents (Optional[str]): 親として追加するフォルダID（カンマ区切り）。
        remove_parents (Optional[str]): 親から削除するフォルダID（カンマ区切り）。
        starred (Optional[bool]): ファイルにスターを付ける/外すかどうか。
        trashed (Optional[bool]): ファイルをゴミ箱に移動/ゴミ箱から戻すかどうか。
        writers_can_share (Optional[bool]): 編集者がファイルを共有できるかどうか。
        copy_requires_writer_permission (Optional[bool]): コピーに編集者の権限が必要かどうか。
        properties (Optional[dict]): ファイルのカスタムキー・値プロパティ。

    Returns:
        str: 適用された更新の詳細を含む確認メッセージ。
    """
    logger.info(f"[update_drive_file] Updating file {file_id} for {user_google_email}")

    current_file_fields = (
        "name, description, mimeType, parents, starred, trashed, webViewLink, "
        "writersCanShare, copyRequiresWriterPermission, properties"
    )
    resolved_file_id, current_file = await resolve_drive_item(
        service,
        file_id,
        extra_fields=current_file_fields,
    )
    file_id = resolved_file_id

    # Build the update body with only specified fields
    update_body = {}
    if name is not None:
        update_body["name"] = name
    if description is not None:
        update_body["description"] = description
    if mime_type is not None:
        update_body["mimeType"] = mime_type
    if starred is not None:
        update_body["starred"] = starred
    if trashed is not None:
        update_body["trashed"] = trashed
    if writers_can_share is not None:
        update_body["writersCanShare"] = writers_can_share
    if copy_requires_writer_permission is not None:
        update_body["copyRequiresWriterPermission"] = copy_requires_writer_permission
    if properties is not None:
        update_body["properties"] = properties

    async def _resolve_parent_arguments(parent_arg: Optional[str]) -> Optional[str]:
        if not parent_arg:
            return None
        parent_ids = [part.strip() for part in parent_arg.split(",") if part.strip()]
        if not parent_ids:
            return None

        resolved_ids = []
        for parent in parent_ids:
            resolved_parent = await resolve_folder_id(service, parent)
            resolved_ids.append(resolved_parent)
        return ",".join(resolved_ids)

    resolved_add_parents = await _resolve_parent_arguments(add_parents)
    resolved_remove_parents = await _resolve_parent_arguments(remove_parents)

    # Build query parameters for parent changes
    query_params = {
        "fileId": file_id,
        "supportsAllDrives": True,
        "fields": "id, name, description, mimeType, parents, starred, trashed, webViewLink, writersCanShare, copyRequiresWriterPermission, properties",
    }

    if resolved_add_parents:
        query_params["addParents"] = resolved_add_parents
    if resolved_remove_parents:
        query_params["removeParents"] = resolved_remove_parents

    # Only include body if there are updates
    if update_body:
        query_params["body"] = update_body

    # Perform the update
    updated_file = await asyncio.to_thread(
        service.files().update(**query_params).execute
    )

    # Build response message
    output_parts = [
        f"✅ Successfully updated file: {updated_file.get('name', current_file['name'])}"
    ]
    output_parts.append(f"   File ID: {file_id}")

    # Report what changed
    changes = []
    if name is not None and name != current_file.get("name"):
        changes.append(f"   • Name: '{current_file.get('name')}' → '{name}'")
    if description is not None:
        old_desc_value = current_file.get("description")
        new_desc_value = description
        should_report_change = (old_desc_value or "") != (new_desc_value or "")
        if should_report_change:
            old_desc_display = (
                old_desc_value if old_desc_value not in (None, "") else "(empty)"
            )
            new_desc_display = (
                new_desc_value if new_desc_value not in (None, "") else "(empty)"
            )
            changes.append(f"   • Description: {old_desc_display} → {new_desc_display}")
    if add_parents:
        changes.append(f"   • Added to folder(s): {add_parents}")
    if remove_parents:
        changes.append(f"   • Removed from folder(s): {remove_parents}")
    current_starred = current_file.get("starred")
    if starred is not None and starred != current_starred:
        star_status = "starred" if starred else "unstarred"
        changes.append(f"   • File {star_status}")
    current_trashed = current_file.get("trashed")
    if trashed is not None and trashed != current_trashed:
        trash_status = "moved to trash" if trashed else "restored from trash"
        changes.append(f"   • File {trash_status}")
    current_writers_can_share = current_file.get("writersCanShare")
    if writers_can_share is not None and writers_can_share != current_writers_can_share:
        share_status = "can" if writers_can_share else "cannot"
        changes.append(f"   • Writers {share_status} share the file")
    current_copy_requires_writer_permission = current_file.get(
        "copyRequiresWriterPermission"
    )
    if (
        copy_requires_writer_permission is not None
        and copy_requires_writer_permission != current_copy_requires_writer_permission
    ):
        copy_status = (
            "requires" if copy_requires_writer_permission else "doesn't require"
        )
        changes.append(f"   • Copying {copy_status} writer permission")
    if properties:
        changes.append(f"   • Updated custom properties: {properties}")

    if changes:
        output_parts.append("")
        output_parts.append("Changes applied:")
        output_parts.extend(changes)
    else:
        output_parts.append("   (No changes were made)")

    output_parts.append("")
    output_parts.append(f"View file: {updated_file.get('webViewLink', '#')}")

    return "\n".join(output_parts)


@server.tool()
@handle_http_errors("get_drive_shareable_link", is_read_only=True, service_type="drive")
@require_google_service("drive", "drive_read")
async def get_drive_shareable_link(
    service,
    user_google_email: str,
    file_id: str,
) -> str:
    """
    Googleドライブのファイルまたはフォルダの共有可能なリンクを取得します。

    Args:
        user_google_email (str): ユーザーのGoogleメールアドレス。必須。
        file_id (str): 共有可能なリンクを取得するファイルまたはフォルダのID。必須。

    Returns:
        str: 共有可能なリンクと現在の共有ステータス。
    """
    logger.info(
        f"[get_drive_shareable_link] Invoked. Email: '{user_google_email}', File ID: '{file_id}'"
    )

    resolved_file_id, _ = await resolve_drive_item(service, file_id)
    file_id = resolved_file_id

    file_metadata = await asyncio.to_thread(
        service.files()
        .get(
            fileId=file_id,
            fields="id, name, mimeType, webViewLink, webContentLink, shared, "
            "permissions(id, type, role, emailAddress, domain, expirationTime)",
            supportsAllDrives=True,
        )
        .execute
    )

    output_parts = [
        f"File: {file_metadata.get('name', 'Unknown')}",
        f"ID: {file_id}",
        f"Type: {file_metadata.get('mimeType', 'Unknown')}",
        f"Shared: {file_metadata.get('shared', False)}",
        "",
        "Links:",
        f"  View: {file_metadata.get('webViewLink', 'N/A')}",
    ]

    web_content_link = file_metadata.get("webContentLink")
    if web_content_link:
        output_parts.append(f"  Download: {web_content_link}")

    permissions = file_metadata.get("permissions", [])
    if permissions:
        output_parts.append("")
        output_parts.append("Current permissions:")
        for perm in permissions:
            output_parts.append(f"  - {format_permission_info(perm)}")

    return "\n".join(output_parts)


@server.tool()
@handle_http_errors("share_drive_file", is_read_only=False, service_type="drive")
@require_google_service("drive", "drive_file")
async def share_drive_file(
    service,
    user_google_email: str,
    file_id: str,
    share_with: Optional[str] = None,
    role: str = "reader",
    share_type: str = "user",
    send_notification: bool = True,
    email_message: Optional[str] = None,
    expiration_time: Optional[str] = None,
    allow_file_discovery: Optional[bool] = None,
) -> str:
    """
    Googleドライブのファイルまたはフォルダを、ユーザー、グループ、ドメイン、またはリンクを知る全員と共有します。

    フォルダを共有する場合、中のすべてのファイルが権限を継承します。

    Args:
        user_google_email (str): ユーザーのGoogleメールアドレス。必須。
        file_id (str): 共有するファイルまたはフォルダのID。必須。
        share_with (Optional[str]): メールアドレス（ユーザー/グループ用）、ドメイン名（ドメイン用）、または 'anyone' の場合は省略。
        role (str): 権限ロール - 'reader', 'commenter', または 'writer'。デフォルトは 'reader' です。
        share_type (str): 共有タイプ - 'user', 'group', 'domain', または 'anyone'。デフォルトは 'user' です。
        send_notification (bool): 通知メールを送信するかどうか。デフォルトはTrueです。
        email_message (Optional[str]): 通知メール用のカスタムメッセージ。
        expiration_time (Optional[str]): RFC 3339形式の有効期限（例: "2025-01-15T00:00:00Z"）。この時間を過ぎると権限は自動的に取り消されます。
        allow_file_discovery (Optional[bool]): 'domain' または 'anyone' 共有の場合 - 検索でファイルを見つけられるようにするかどうか。デフォルトはNone（APIデフォルト）。

    Returns:
        str: 権限の詳細と共有可能なリンクを含む確認。
    """
    logger.info(
        f"[share_drive_file] Invoked. Email: '{user_google_email}', File ID: '{file_id}', Share with: '{share_with}', Role: '{role}', Type: '{share_type}'"
    )

    validate_share_role(role)
    validate_share_type(share_type)

    if share_type in ("user", "group") and not share_with:
        raise ValueError(f"share_with is required for share_type '{share_type}'")
    if share_type == "domain" and not share_with:
        raise ValueError("share_with (domain name) is required for share_type 'domain'")

    resolved_file_id, file_metadata = await resolve_drive_item(
        service, file_id, extra_fields="name, webViewLink"
    )
    file_id = resolved_file_id

    permission_body = {
        "type": share_type,
        "role": role,
    }

    if share_type in ("user", "group"):
        permission_body["emailAddress"] = share_with
    elif share_type == "domain":
        permission_body["domain"] = share_with

    if expiration_time:
        validate_expiration_time(expiration_time)
        permission_body["expirationTime"] = expiration_time

    if share_type in ("domain", "anyone") and allow_file_discovery is not None:
        permission_body["allowFileDiscovery"] = allow_file_discovery

    create_params = {
        "fileId": file_id,
        "body": permission_body,
        "supportsAllDrives": True,
        "fields": "id, type, role, emailAddress, domain, expirationTime",
    }

    if share_type in ("user", "group"):
        create_params["sendNotificationEmail"] = send_notification
        if email_message:
            create_params["emailMessage"] = email_message

    created_permission = await asyncio.to_thread(
        service.permissions().create(**create_params).execute
    )

    output_parts = [
        f"Successfully shared '{file_metadata.get('name', 'Unknown')}'",
        "",
        "Permission created:",
        f"  - {format_permission_info(created_permission)}",
        "",
        f"View link: {file_metadata.get('webViewLink', 'N/A')}",
    ]

    return "\n".join(output_parts)


@server.tool()
@handle_http_errors("batch_share_drive_file", is_read_only=False, service_type="drive")
@require_google_service("drive", "drive_file")
async def batch_share_drive_file(
    service,
    user_google_email: str,
    file_id: str,
    recipients: List[Dict[str, Any]],
    send_notification: bool = True,
    email_message: Optional[str] = None,
) -> str:
    """
    単一の操作でGoogleドライブのファイルまたはフォルダを複数のユーザーまたはグループと共有します。

    各受信者は異なるロールとオプションの有効期限を持つことができます。

    注: 各受信者は順番に処理されます。受信者リストが非常に大きい場合は、複数の呼び出しに分割することを検討してください。

    Args:
        user_google_email (str): ユーザーのGoogleメールアドレス。必須。
        file_id (str): 共有するファイルまたはフォルダのID。必須。
        recipients (List[Dict]): 受信者オブジェクトのリスト。各オブジェクトには以下を含める必要があります:
            - email (str): 受信者のメールアドレス。'user' または 'group' share_type の場合に必須。
            - role (str): 権限ロール - 'reader', 'commenter', または 'writer'。デフォルトは 'reader' です。
            - share_type (str, optional): 'user', 'group', または 'domain'。デフォルトは 'user' です。
            - expiration_time (str, optional): RFC 3339形式の有効期限（例: "2025-01-15T00:00:00Z"）。
            ドメイン共有の場合、'email' の代わりに 'domain' フィールドを使用します:
            - domain (str): ドメイン名。share_type が 'domain' の場合に必須。
        send_notification (bool): 通知メールを送信するかどうか。デフォルトはTrueです。
        email_message (Optional[str]): 通知メール用のカスタムメッセージ。

    Returns:
        str: 各受信者の成功/失敗を含む作成された権限の概要。
    """
    logger.info(
        f"[batch_share_drive_file] Invoked. Email: '{user_google_email}', File ID: '{file_id}', Recipients: {len(recipients)}"
    )

    resolved_file_id, file_metadata = await resolve_drive_item(
        service, file_id, extra_fields="name, webViewLink"
    )
    file_id = resolved_file_id

    if not recipients:
        raise ValueError("recipients list cannot be empty")

    results = []
    success_count = 0
    failure_count = 0

    for recipient in recipients:
        share_type = recipient.get("share_type", "user")

        if share_type == "domain":
            domain = recipient.get("domain")
            if not domain:
                results.append("  - Skipped: missing domain for domain share")
                failure_count += 1
                continue
            identifier = domain
        else:
            email = recipient.get("email")
            if not email:
                results.append("  - Skipped: missing email address")
                failure_count += 1
                continue
            identifier = email

        role = recipient.get("role", "reader")
        try:
            validate_share_role(role)
        except ValueError as e:
            results.append(f"  - {identifier}: Failed - {e}")
            failure_count += 1
            continue

        try:
            validate_share_type(share_type)
        except ValueError as e:
            results.append(f"  - {identifier}: Failed - {e}")
            failure_count += 1
            continue

        permission_body = {
            "type": share_type,
            "role": role,
        }

        if share_type == "domain":
            permission_body["domain"] = identifier
        else:
            permission_body["emailAddress"] = identifier

        if recipient.get("expiration_time"):
            try:
                validate_expiration_time(recipient["expiration_time"])
                permission_body["expirationTime"] = recipient["expiration_time"]
            except ValueError as e:
                results.append(f"  - {identifier}: Failed - {e}")
                failure_count += 1
                continue

        create_params = {
            "fileId": file_id,
            "body": permission_body,
            "supportsAllDrives": True,
            "fields": "id, type, role, emailAddress, domain, expirationTime",
        }

        if share_type in ("user", "group"):
            create_params["sendNotificationEmail"] = send_notification
            if email_message:
                create_params["emailMessage"] = email_message

        try:
            created_permission = await asyncio.to_thread(
                service.permissions().create(**create_params).execute
            )
            results.append(f"  - {format_permission_info(created_permission)}")
            success_count += 1
        except HttpError as e:
            results.append(f"  - {identifier}: Failed - {str(e)}")
            failure_count += 1

    output_parts = [
        f"Batch share results for '{file_metadata.get('name', 'Unknown')}'",
        "",
        f"Summary: {success_count} succeeded, {failure_count} failed",
        "",
        "Results:",
    ]
    output_parts.extend(results)
    output_parts.extend(
        [
            "",
            f"View link: {file_metadata.get('webViewLink', 'N/A')}",
        ]
    )

    return "\n".join(output_parts)


@server.tool()
@handle_http_errors("update_drive_permission", is_read_only=False, service_type="drive")
@require_google_service("drive", "drive_file")
async def update_drive_permission(
    service,
    user_google_email: str,
    file_id: str,
    permission_id: str,
    role: Optional[str] = None,
    expiration_time: Optional[str] = None,
) -> str:
    """
    Googleドライブのファイルまたはフォルダの既存の権限を更新します。

    Args:
        user_google_email (str): ユーザーのGoogleメールアドレス。必須。
        file_id (str): ファイルまたはフォルダのID。必須。
        permission_id (str): 更新する権限のID（get_drive_file_permissionsから取得）。必須。
        role (Optional[str]): 新しいロール - 'reader', 'commenter', または 'writer'。指定されない場合、ロールは変更されません。
        expiration_time (Optional[str]): RFC 3339形式の有効期限（例: "2025-01-15T00:00:00Z"）。権限の有効期限を設定または更新します。

    Returns:
        str: 更新された権限の詳細を含む確認。
    """
    logger.info(
        f"[update_drive_permission] Invoked. Email: '{user_google_email}', File ID: '{file_id}', Permission ID: '{permission_id}', Role: '{role}'"
    )

    if not role and not expiration_time:
        raise ValueError("Must provide at least one of: role, expiration_time")

    if role:
        validate_share_role(role)
    if expiration_time:
        validate_expiration_time(expiration_time)

    resolved_file_id, file_metadata = await resolve_drive_item(
        service, file_id, extra_fields="name"
    )
    file_id = resolved_file_id

    # Google API requires role in update body, so fetch current if not provided
    if not role:
        current_permission = await asyncio.to_thread(
            service.permissions()
            .get(
                fileId=file_id,
                permissionId=permission_id,
                supportsAllDrives=True,
                fields="role",
            )
            .execute
        )
        role = current_permission.get("role")

    update_body = {"role": role}
    if expiration_time:
        update_body["expirationTime"] = expiration_time

    updated_permission = await asyncio.to_thread(
        service.permissions()
        .update(
            fileId=file_id,
            permissionId=permission_id,
            body=update_body,
            supportsAllDrives=True,
            fields="id, type, role, emailAddress, domain, expirationTime",
        )
        .execute
    )

    output_parts = [
        f"Successfully updated permission on '{file_metadata.get('name', 'Unknown')}'",
        "",
        "Updated permission:",
        f"  - {format_permission_info(updated_permission)}",
    ]

    return "\n".join(output_parts)


@server.tool()
@handle_http_errors("remove_drive_permission", is_read_only=False, service_type="drive")
@require_google_service("drive", "drive_file")
async def remove_drive_permission(
    service,
    user_google_email: str,
    file_id: str,
    permission_id: str,
) -> str:
    """
    Googleドライブのファイルまたはフォルダから権限を削除し、アクセス権を取り消します。

    Args:
        user_google_email (str): ユーザーのGoogleメールアドレス。必須。
        file_id (str): ファイルまたはフォルダのID。必須。
        permission_id (str): 削除する権限のID（get_drive_file_permissionsから取得）。必須。

    Returns:
        str: 権限削除の確認。
    """
    logger.info(
        f"[remove_drive_permission] Invoked. Email: '{user_google_email}', File ID: '{file_id}', Permission ID: '{permission_id}'"
    )

    resolved_file_id, file_metadata = await resolve_drive_item(
        service, file_id, extra_fields="name"
    )
    file_id = resolved_file_id

    await asyncio.to_thread(
        service.permissions()
        .delete(fileId=file_id, permissionId=permission_id, supportsAllDrives=True)
        .execute
    )

    output_parts = [
        f"Successfully removed permission from '{file_metadata.get('name', 'Unknown')}'",
        "",
        f"Permission ID '{permission_id}' has been revoked.",
    ]

    return "\n".join(output_parts)


@server.tool()
@handle_http_errors(
    "transfer_drive_ownership", is_read_only=False, service_type="drive"
)
@require_google_service("drive", "drive_file")
async def transfer_drive_ownership(
    service,
    user_google_email: str,
    file_id: str,
    new_owner_email: str,
    move_to_new_owners_root: bool = False,
) -> str:
    """
    Googleドライブのファイルまたはフォルダの所有権を別のユーザーに転送します。

    これは不可逆的な操作です。現在の所有者は編集者になります。
    同じGoogle Workspaceドメイン内、または個人アカウント間でのみ機能します。

    Args:
        user_google_email (str): ユーザーのGoogleメールアドレス。必須。
        file_id (str): 転送するファイルまたはフォルダのID。必須。
        new_owner_email (str): 新しい所有者のメールアドレス。必須。
        move_to_new_owners_root (bool): Trueの場合、ファイルを新しい所有者のマイドライブのルートに移動します。デフォルトはFalseです。

    Returns:
        str: 所有権転送の確認。
    """
    logger.info(
        f"[transfer_drive_ownership] Invoked. Email: '{user_google_email}', File ID: '{file_id}', New owner: '{new_owner_email}'"
    )

    resolved_file_id, file_metadata = await resolve_drive_item(
        service, file_id, extra_fields="name, owners"
    )
    file_id = resolved_file_id

    current_owners = file_metadata.get("owners", [])
    current_owner_emails = [o.get("emailAddress", "") for o in current_owners]

    permission_body = {
        "type": "user",
        "role": "owner",
        "emailAddress": new_owner_email,
    }

    await asyncio.to_thread(
        service.permissions()
        .create(
            fileId=file_id,
            body=permission_body,
            transferOwnership=True,
            moveToNewOwnersRoot=move_to_new_owners_root,
            supportsAllDrives=True,
            fields="id, type, role, emailAddress",
        )
        .execute
    )

    output_parts = [
        f"Successfully transferred ownership of '{file_metadata.get('name', 'Unknown')}'",
        "",
        f"New owner: {new_owner_email}",
        f"Previous owner(s): {', '.join(current_owner_emails) or 'Unknown'}",
    ]

    if move_to_new_owners_root:
        output_parts.append(f"File moved to {new_owner_email}'s My Drive root.")

    output_parts.extend(["", "Note: Previous owner now has editor access."])

    return "\n".join(output_parts)
