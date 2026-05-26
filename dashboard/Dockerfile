FROM node:20-alpine AS deps
WORKDIR /app
COPY package.json ./
RUN npm install --no-audit --no-fund

FROM node:20-alpine AS build
WORKDIR /app
ARG NEXT_PUBLIC_LIGHTSEI_API_URL=http://localhost:8000
ARG NEXT_PUBLIC_LIGHTSEI_API_KEY=demo-key
ENV NEXT_PUBLIC_LIGHTSEI_API_URL=$NEXT_PUBLIC_LIGHTSEI_API_URL
ENV NEXT_PUBLIC_LIGHTSEI_API_KEY=$NEXT_PUBLIC_LIGHTSEI_API_KEY
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
# Phase 26.4 + retroactive 21.4 fix: Next.js standalone output does
# NOT auto-include public/. Without this copy, /sw.js, /widget.js,
# /apple-touch-icon.png, /icon-*.png all 404 in prod. The widget.js
# bug has been latent since Phase 21.4; the PWA install path needs
# all four icon paths + manifest + sw.js, which is what surfaced it.
COPY --from=build /app/public ./public
EXPOSE 3000
CMD ["node", "server.js"]
