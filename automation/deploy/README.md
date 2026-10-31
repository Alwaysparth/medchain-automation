# Deploy the MedChain automation stack to Vultr (Member 2)

What runs on the server: **n8n** (public, HTTPS via Caddy) · **ai-worker** and **mock-api** (private) · files in **Vultr Object Storage**.

```
Internet ──HTTPS──> Caddy :443 ──> n8n :5678 ──(internal network)──> ai-worker :8000
                                                                └──> mock-api :8100  (until Member 1/3's services exist)
ai-worker / mock-api ──S3 API──> Vultr Object Storage (private bucket)
```

Time needed: ~30 minutes. Commands for **your laptop** are PowerShell; commands **on the server** are Linux.

---

## 1. Create the Vultr resources (web console)

**a) Server:** Products → Compute → **Deploy** → *Cloud Compute (shared CPU)*
- Location: **Mumbai, Bangalore or Delhi NCR** (closest to users; keep everything in one region)
- Image: **Ubuntu 24.04 LTS x64**
- Plan: at least **2 GB RAM** (4 GB is comfortable for n8n + image processing)
- SSH key: add one (recommended), or use the root password Vultr shows
- Hostname: `medchain-automation` → **Deploy**. Copy the server's **IP address**.

**b) Object Storage:** Products → **Object Storage** → Add → choose a location (same region if offered) → label `medchain` → wait until *Active*. Then:
- Copy **Hostname**, **Access Key**, **Secret Key** (Overview tab)
- **Buckets → Create bucket**: e.g. `medchain-docs-<teamname>` (names are shared across all customers, so make it unique). Keep it **private**.

**c) A name for HTTPS:**
- If you own a domain: add a DNS **A record** `n8n.yourdomain.com → <server IP>`.
- If not: use **`<server-ip>.sslip.io`** (e.g. `203.0.113.10.sslip.io`). It points to your IP automatically and works with free HTTPS.

## 2. Copy the code to the server

From the repo root on your laptop (zip contains `contracts/`, `automation/`, `.dockerignore`):
```powershell
scp medchain-member2-deploy.zip root@<server-ip>:/root/
ssh root@<server-ip>
```

On the server:
```bash
apt-get update && apt-get install -y unzip
mkdir -p /opt/medchain && unzip -o /root/medchain-member2-deploy.zip -d /opt/medchain
bash /opt/medchain/automation/deploy/setup-server.sh      # Docker, firewall (22/80/443 only), swap
```

## 3. Create `.env` on the server

```bash
cd /opt/medchain/automation
cp .env.example .env
echo "INTERNAL_SERVICE_TOKEN=$(openssl rand -hex 32)" >> .env    # new secrets for production,
echo "N8N_WEBHOOK_TOKEN=$(openssl rand -hex 32)" >> .env         # different from your laptop's
nano .env
```
In nano (later lines win, so the two tokens appended above replace the `change-me` ones). Set:
```
GEMINI_API_KEY=<your key>
STORAGE_BACKEND=s3
S3_ENDPOINT_URL=https://<Hostname from step 1b>
S3_ACCESS_KEY=<Access Key>
S3_SECRET_KEY=<Secret Key>
S3_BUCKET=<bucket name>
COMPOSE_FILE=docker-compose.yml:deploy/docker-compose.prod.yml
N8N_DOMAIN=<n8n.yourdomain.com or IP.sslip.io>
ACME_EMAIL=<your email>
```
Save: `Ctrl+O`, `Enter`, `Ctrl+X`. Then lock it down: `chmod 600 .env`

## 4. Start

```bash
docker compose up -d --build
docker compose ps                                   # caddy, n8n, ai-worker, mock-api: running
docker compose exec ai-worker python storage.py    # expect: backend: S3Storage ... put/get/delete: OK
curl -s http://127.0.0.1:8000/health               # "storage": true, "gemini_key_set": true
docker compose logs caddy | grep -i certificate    # "certificate obtained successfully"
```

## 5. Set up n8n — do this IMMEDIATELY

> The first person to open a fresh n8n becomes its owner. Open it right after starting.

1. Open `https://<N8N_DOMAIN>` → create the owner account with a **strong password**.
2. Import and activate the workflow:
   ```bash
   docker compose exec n8n n8n import:workflow --input=/workflows/upload-pipeline.json
   ```
   Refresh n8n → open **MedChain – upload pipeline** → **Active / Publish**.

## 6. Test the deployment

**Public webhook + HTTPS** (from your laptop):
```powershell
curl.exe -X POST https://<N8N_DOMAIN>/webhook/upload-pipeline -H "Content-Type: application/json" -d "{}"
```
Expect `400 {"accepted":false,...}`: HTTPS works, n8n is reachable, and the token check protects it.

**Full pipeline through a private SSH tunnel** (the mock API is not public):
```powershell
ssh -L 18100:127.0.0.1:8100 root@<server-ip>      # keep this window open
```
In a browser on your laptop: http://localhost:18100/docs → `POST /demo/upload` → upload `fake_lab_report_01.png`.
Then http://localhost:18100/demo/entries → `completed`. In the Vultr console the bucket now has `raw/`, `compressed/`, `thumbs/`.

## Updating after code changes

Laptop: `scp <new zip> root@<server-ip>:/root/` · Server:
```bash
unzip -o /root/<new zip> -d /opt/medchain && cd /opt/medchain/automation && docker compose up -d --build
```
`.env` and all data (n8n workflows, traces, mock DB) survive updates: they live in Docker volumes. If a workflow JSON changed, re-import it and re-activate.

## Everyday commands (on the server, in /opt/medchain/automation)

| Task | Command |
|---|---|
| Status | `docker compose ps` |
| Logs (live) | `docker compose logs -f n8n ai-worker` |
| Restart one service | `docker compose restart ai-worker` |
| AI traces | `docker compose exec ai-worker cat /data/traces.jsonl` |
| Back up n8n | `docker run --rm -v medchain-automation_n8n_data:/d -v /root:/b alpine tar czf /b/n8n-backup.tgz -C /d .` |
| Stop everything | `docker compose down` (data kept) |

## Security checklist

- [ ] Only ports 22, 80, 443 open (`ufw status`); ai-worker / mock-api / n8n ports bound to 127.0.0.1
- [ ] Production tokens differ from laptop tokens; `.env` is `chmod 600` and never committed
- [ ] Gemini key: use a **fresh** key (the old one was shared in chat)
- [ ] Bucket is private; n8n owner password is strong
- [ ] Only FAKE demo data on this server
- [ ] When Member 1's API is live: set `MEDCHAIN_API_URL` (and `SOLANA_CLIENT_URL` for Member 3), remove `mock-api`, `docker compose up -d`
