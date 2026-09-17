FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS builder
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32
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
