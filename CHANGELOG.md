# Google Workspace MCP 変更履歴

## 2026-01-05

### 日本語化対応 (Localization)

#### ツール説明翻訳 (Tool Descriptions)
Google Calendar, Google Drive, Google Docs, および Core Server の全38個のMCPツールの説明文（docstrings）を日本語に翻訳しました。これにより、AIエージェントおよびユーザーがツールの機能をより正確に理解できるようになります。

- **`gcalendar/calendar_tools.py`**: 全カレンダー操作ツールの説明を日本語化
- **`gdrive/drive_tools.py`**: 全ドライブ操作ツールの説明を日本語化
- **`gdocs/docs_tools.py`**: 全ドキュメント操作ツールの説明を日本語化
- **`core/server.py`**: `start_google_auth` ツールの説明を日本語化

### セキュリティ・プライバシー強化

#### `auth/scopes.py`
- `BASE_SCOPES` から `userinfo.email` と `userinfo.profile` を削除
- `openid` のみに変更してプライバシーを保護

```diff
- BASE_SCOPES = [USERINFO_EMAIL_SCOPE, USERINFO_PROFILE_SCOPE, OPENID_SCOPE]
+ BASE_SCOPES = [OPENID_SCOPE]
```

#### `auth/google_auth.py`
- `handle_auth_callback()` を修正
- single-userモードでは `get_user_info()` をスキップ
- プレースホルダーメール `single-user@localhost` を使用

### 設定ファイル

#### `start-mcp.ps1` (新規作成)
- 1Password連携によるセキュアな認証情報取得
- `MCP_SINGLE_USER_MODE=1` 環境変数追加
- Calendar/Drive/Docs 機能の有効化
