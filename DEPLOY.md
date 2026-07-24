# Sahu CRM — No-VPS Deployment Guide (Render + Atlas + R2 + Hostinger)

Koi server maintain nahi karna. Sab managed services, sabme free tier hai (backend ke liye ek chhota paid plan recommend hai, taki AI Receptionist/Payments 24x7 fast respond karein).

## Overview
| Part | Service | Cost |
|---|---|---|
| Database | MongoDB Atlas | Free |
| Backend (FastAPI) | Render.com | Free (test) ya ~$7/mo (Starter, recommended for live business) |
| File uploads | Cloudflare R2 | Free (10GB) |
| Frontend (React) | Hostinger Cloud — `crm.indorejyotish.in` | Free (aapke plan me already included) |

---

## Step 1 — MongoDB Atlas (Database)
1. https://cloud.mongodb.com → free account banao
2. "Build a Database" → **M0 Free** cluster → region: Mumbai (ap-south-1)
3. Database Access → user banao (username/password note kar lo)
4. Network Access → "Allow access from anywhere" (0.0.0.0/0) — Render ka fixed IP nahi hota
5. "Connect" → "Drivers" → connection string copy karo, e.g.:
   ```
   mongodb+srv://USERNAME:PASSWORD@cluster0.xxxxx.mongodb.net
   ```
   Ye `.env` me `MONGO_URL` banega.

## Step 2 — Cloudflare R2 (File storage)
1. https://dash.cloudflare.com → R2 → "Create bucket" → name: `sahu-crm-uploads`
2. R2 → "Manage API Tokens" → "Create API Token" → permissions: Object Read & Write, is bucket tak scope karo
3. Note kar lo: **Account ID**, **Access Key ID**, **Secret Access Key**

## Step 3 — Render.com (Backend)
1. https://render.com → GitHub se sign up, apna `sahu-crm-main` repo connect karo
2. "New +" → "Web Service" → repo select karo, **Root Directory: `backend`**
3. Settings:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn server:app --host 0.0.0.0 --port $PORT`
   - Instance Type: Free (test ke liye) ya Starter ($7/mo, live business ke liye recommended — free tier sleep ho jaata hai aur AI chat/payment slow ho jayega)
4. Environment tab me `.env.example` ke saare variables add karo (`MONGO_URL`, `JWT_SECRET`, `GEMINI_API_KEY`, `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME`, `CORS_ORIGINS=https://crm.indorejyotish.in`)
5. Deploy karo. Render aapko ek URL dega, e.g. `https://sahu-crm.onrender.com` — ye backend URL hai.
6. Future updates: `git push` karte hi Render auto-deploy kar dega.

## Step 4 — Frontend build (local machine ya Claude se)
```bash
cd frontend
echo "REACT_APP_BACKEND_URL=https://sahu-crm.onrender.com" > .env
yarn install && yarn build
```
Ye `frontend/build/` folder banayega — isi ko upload karna hai.

(Chaho to ye build main abhi kar ke tumhe ready folder de sakta hoon — bata dena.)

## Step 5 — Hostinger Cloud pe subdomain banao
1. hPanel → Websites → `bestindore.com` (ya jo bhi account indorejyotish.in ke liye hai) → **Domains → Subdomains**
2. Subdomain: `crm` → Domain: `indorejyotish.in` → Create
3. Isse ek folder banega, jaise `public_html/crm/`

## Step 6 — Frontend upload (FTP)
FTP details (aapke screenshot se):
- Host: `ftp://bestindore.com`
- Username: `u449141075.bestindore.com`
- Path: `public_html/crm/` (subdomain ka folder)

FileZilla (free FTP app) se `frontend/build/` ke andar ki saari files is folder me upload kar do.

## Step 7 — Test
`https://crm.indorejyotish.in` khol ke check karo. Login/Dashboard aana chahiye.

---

## Ab kya baaki hai
| Feature | Status |
|---|---|
| Auto Pay (Razorpay) | Structure ready — keys dalne hain |
| Auto Chat (AI Receptionist) | Fixed — Gemini key chahiye |
| File uploads | Fixed — R2 pe jayenge |
| Follow-up automation | Backend service hai, scheduler jodna baaki |
| Call automation/button | Design pending — click-to-call button ya WhatsApp-call, bata dena |

Bata do agar chaho to frontend build abhi yahi bana ke de doon, taaki seedha FTP upload kar sako.
