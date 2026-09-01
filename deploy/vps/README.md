# simverse.space VPS deployment

Production uses one public origin:

- Website: `https://simverse.space`
- API and WebSocket gateway: `https://simverse.space/api`
- Backend origin: loopback-only `127.0.0.1:8100`
- Robinhood Chain: chain ID `4663`
- Agent Registry proxy: `0x24f6f6bE48066cbE0B54d741cd4B52862Bb4b05c`

Deploy the backend Compose bundle to `/opt/skills-world`, build the frontend with `frontend/.env.production.example`, and publish `frontend/dist` to `/var/www/simverse`. Install the HTTP Nginx template first, obtain the `simverse.space` + `www.simverse.space` certificate with Certbot webroot `/var/www/letsencrypt`, then install the HTTPS template.

Keep `WEB3_PUBLIC_API_BASE_URL=https://simverse.space/api` in the backend production environment. Upload, memory, and save URIs are written on-chain, so this public prefix must remain reachable after deployment.

Do not expose ports `5432`, `6379`, or `8100` publicly. Do not copy `私钥.txt`, `VPS.txt`, any `.env`, or wallet content into the public web root.
