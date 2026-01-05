# Google Workspace MCP 変更履歴

## 2026-01-05

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
- Calendar/Drive のみに機能制限
