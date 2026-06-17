# migration.vars.example.ps1 — COMMITTED TEMPLATE (placeholders only; safe to commit).
#
# Copy this to migration.vars.local.ps1, fill in the REAL values, then dot-source it.
# migration.vars.local.ps1 is git-ignored (see repo .gitignore) — never commit or mirror it.
#
#   Copy-Item .\migration.vars.example.ps1 .\migration.vars.local.ps1
#   # edit migration.vars.local.ps1 with real values
#   . .\migration.vars.local.ps1

# --- Tableau source (D1=A live pull) ---
$SITE_URL  = "<pod>.online.tableau.com"   # Tableau host, e.g. 10ay.online.tableau.com (no https://)
$SITE_NAME = "<site-content-url>"          # the site slug in the URL; "" for Default

# --- Auth: PAT (D5=A, default) ---
$PAT_NAME  = "<pat-name>"                   # Personal Access Token name

# --- Secret source: Azure Key Vault (holds the PAT or Connected-App secret value) ---
$KV_NAME     = "<your-key-vault>"           # Key Vault name
$SECRET_NAME = "<pat-secret-name>"          # secret whose value is the PAT secret

# --- Fabric target (STEP 3 deploy; omit if D3=C / local only) ---
$FABRIC_WORKSPACE = "<fabric-workspace>"    # target Fabric workspace name or GUID

# --- Connected App JWT (D5=B only) — leave as-is unless using JWT ---
$CA_CLIENT_ID = "<connected-app-client-id>"
$CA_SECRET_ID = "<connected-app-secret-id>"
$JWT_USERNAME = "<user-to-impersonate>"     # Site Admin to bypass RLS
