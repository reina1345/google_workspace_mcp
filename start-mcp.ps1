# Google Workspace MCP スタートスクリプト
# 1Passwordから認証情報を取得してMCPサーバーを起動

# 1Passwordから認証情報を取得
$env:GOOGLE_OAUTH_CLIENT_ID = op read "op://Personal/Google OAuth Antigravity/Client ID"
$env:GOOGLE_OAUTH_CLIENT_SECRET = op read "op://Personal/Google OAuth Antigravity/Client Secret"
$env:OAUTHLIB_INSECURE_TRANSPORT = "1"
$env:MCP_SINGLE_USER_MODE = "1"

# MCPサーバー起動
Set-Location $PSScriptRoot
uv run python main.py --single-user --tools calendar drive docs
