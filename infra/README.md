# Deploying to Azure

Two containers on one App Service plan, a PostgreSQL server, and an Azure
Files share. About $32/month in Central India.

```
Internet ──https──┐
                  │
   ┌──────────────┴────────────────────────────┐
   │  App Service Plan  (Linux B1, 1 instance) │
   │                                            │
   │   iragst-web ──calls──► iragst-api        │
   └───────────────────────────┬────────────────┘
                               │
        ┌──────────────────────┼──────────────────┐
        ▼                      ▼                  ▼
   PostgreSQL            Azure Files        Container Registry
   Flexible B1ms         mounted /data      gst-api, gst-web
   accounts, queue,      workbooks, PDFs,
   audit trail           archive
```

## Why it is shaped this way

**One instance, permanently.** `workbook.py` serialises writes with an
in-process lock, which is worth nothing across processes. `singleton.py` takes
a kernel lock so a second process fails loudly rather than silently
overwriting a posted tax row. `numberOfWorkers` is 1 and autoscale is off; both
are correctness constraints, not cost savings.

**PostgreSQL rather than SQLite on the share.** Every persistent disk App
Service offers is SMB-backed, and SQLite's WAL mode needs shared-memory
mapping that SMB does not provide. Disabling WAL makes it work and leaves a
database of tax records one dropped SMB connection from corruption.

**Containers rather than the built-in Python runtime.** OCR needs Tesseract,
which is a system package.

**The master workbook is uploaded before the apps start.** The API refuses to
start without it. Deploy first and upload after, and the container crash-loops
on a missing path — which reads like a broken image rather than a missing file.
`deploy.ps1` does these in the right order.

## First deployment

```powershell
az login
./infra/deploy.ps1 `
    -ResourceGroup ira-gst `
    -MasterWorkbook "D:\IRA\01 Ira Innovations May-26 GST Calculation.xlsx"
```

Roughly 15 minutes, most of it PostgreSQL. Then open the printed `web` URL and
create the first account — it becomes the administrator.

Options:

| Flag | Default | Notes |
|---|---|---|
| `-Prefix` | `iragst` | Prefixes every resource name. Must be globally unique for the registry and storage account. |
| `-Location` | `centralindia` | Keeps Indian tax records in-country. |
| `-DbPassword` | generated | Only the app ever uses it, through an app setting. |
| `-AnthropicKey` | none | Without it the deployment runs the offline reader. |
| `-SkipInfra` | off | Rebuild and push images without touching infrastructure. |

## Bringing an existing database across

The deployment starts empty. To carry over accounts, the queue and the audit
trail from a local `store.db`:

```powershell
$db = az postgres flexible-server show -g ira-gst -n iragst-db --query fullyQualifiedDomainName -o tsv
$env:DATABASE_URL = "postgresql://gstadmin:<password>@$db:5432/gst?sslmode=require"

cd backend
python -m tools.migrate_to_postgres --sqlite data/store.db
```

It refuses to run against a database that already holds rows — the plausible
mistake is running it twice and doubling the audit trail. `--replace` empties
the destination first and is the only supported way to re-run.

Add your own address to the database firewall first, or the connection is
refused:

```powershell
$ip = (Invoke-RestMethod https://api.ipify.org?format=json).ip
az postgres flexible-server firewall-rule create -g ira-gst -n iragst-db `
    --rule-name my-laptop --start-ip-address $ip --end-ip-address $ip
```

Remove it when you are done.

## Deploying a change

`deploy.ps1` again, or push to `master` and let
`.github/workflows/deploy.yml` do it. The workflow runs the test suite against
both SQLite and PostgreSQL before it deploys, and skips deployment entirely if
the Azure secrets are absent.

To enable it, create a service principal and store the three secrets:

```powershell
az ad sp create-for-rbac --name gst-deploy --role contributor `
    --scopes /subscriptions/<id>/resourceGroups/ira-gst --sdk-auth
```

| Secret | Value |
|---|---|
| `AZURE_CREDENTIALS` | the whole JSON blob from that command |
| `AZURE_RESOURCE_GROUP` | `ira-gst` |
| `AZURE_PREFIX` | `iragst` |

## Checking on it

```powershell
# Everything the app checks about itself
curl https://iragst-api.azurewebsites.net/api/ready

# Live logs
az webapp log tail -g ira-gst -n iragst-api
```

`/api/ready` reports the database it is actually talking to, whether the master
workbook was found, and whether the reader has credentials. A reader marked
`degraded` still captures documents through the offline path.

## Trying it locally first

`docker compose up --build` runs the same two images against the same
PostgreSQL version. Copy `.env.example` to `.env` and set
`GST_MASTER_WORKBOOK` to the master workbook's path.

## Known limits

- **Public endpoint.** The app's own accounts are the only barrier. To add an
  IP allowlist: `az webapp config access-restriction add`.
- **The database firewall allows all Azure services**, which is broader than
  this app. Narrowing it to the plan's outbound addresses, or moving to a
  private endpoint, is the next hardening step.
- **The registry uses its admin user.** A managed identity with `AcrPull` is
  better and needs subscription-level permissions a first deployment often
  lacks.
- **No staging slot.** A bad image goes straight to the only environment.
  Images are tagged with the commit as well as `latest`, so rolling back means
  pointing the app at a specific tag.
