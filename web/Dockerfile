FROM node:22-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

FROM node:22-alpine
WORKDIR /app
RUN apk add --no-cache wget
COPY --from=builder /app/node_modules ./node_modules
COPY db ./db
COPY src ./src
COPY views ./views
COPY public ./public
COPY scripts ./scripts
COPY docs ./docs
COPY locales ./locales
COPY package.json ./
RUN chmod +x scripts/start.sh
USER node
EXPOSE 80
CMD ["sh", "scripts/start.sh"]
