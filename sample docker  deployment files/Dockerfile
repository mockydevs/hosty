# Multi-stage build for Next.js application
FROM node:20-alpine AS base

# Install dependencies only when needed
FROM base AS deps
RUN apk add --no-cache libc6-compat openssl ca-certificates
WORKDIR /app

# Enable pnpm
ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"
RUN mkdir -p /pnpm && corepack enable

# Install ALL dependencies (including dev) for build
COPY package.json pnpm-lock.yaml* ./
COPY prisma ./prisma
# Use pnpm for faster, more efficient installs
RUN --mount=type=cache,id=pnpm,target=/pnpm/store pnpm install --frozen-lockfile

# Install production dependencies separately
FROM base AS production-deps
RUN apk add --no-cache libc6-compat openssl ca-certificates
WORKDIR /app

# Enable pnpm
ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"
RUN corepack enable

COPY package.json pnpm-lock.yaml* ./
COPY prisma ./prisma
# Install all dependencies (including dev for Prisma), then prune non-production
# Use --ignore-scripts to prevent postinstall from running after pnpm prune removes prisma CLI
RUN --mount=type=cache,id=pnpm,target=/pnpm/store pnpm install --frozen-lockfile && \
  pnpm prune --prod --ignore-scripts && \
  pnpm store prune

# Rebuild the source code only when needed
FROM base AS builder
WORKDIR /app

# Enable pnpm
ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"
RUN corepack enable

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# Generate Prisma client (already generated in deps stage via postinstall)
# RUN pnpm exec prisma generate

# Set build environment variables
ENV NEXT_TELEMETRY_DISABLED=1
ENV NEXT_BUILD_TIME=true

# Accept non-sensitive build-time configuration used by Next.js build
ARG NEXT_PUBLIC_APP_URL
ARG NEXT_PUBLIC_SITE_URL
ARG SITE_URL
ARG AWS_REGION
ARG AWS_S3_BUCKET_NAME
# TEMPORARILY DISABLED: CloudFront - uncomment when resolved with Amazon
# ARG AWS_CLOUDFRONT_DOMAIN

# Expose as env during build only (no secrets)
ENV NEXT_PUBLIC_APP_URL=$NEXT_PUBLIC_APP_URL
ENV NEXT_PUBLIC_SITE_URL=$NEXT_PUBLIC_SITE_URL
ENV SITE_URL=$SITE_URL
ENV AWS_REGION=$AWS_REGION
ENV AWS_S3_BUCKET_NAME=$AWS_S3_BUCKET_NAME
# TEMPORARILY DISABLED: CloudFront - uncomment when resolved with Amazon
# ENV AWS_CLOUDFRONT_DOMAIN=$AWS_CLOUDFRONT_DOMAIN

# Build the application (no cache mount to ensure fresh builds)
RUN pnpm run build

# Production image
FROM base AS runner
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1

# Create system user and group
RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 nextjs

# Install runtime dependencies
RUN apk add --no-cache openssl curl tini ca-certificates postgresql-client

# Copy the standalone Next.js application
COPY --from=builder --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /app/public ./public

# Copy full node_modules from builder (includes prisma CLI needed for migrations)
COPY --from=builder --chown=nextjs:nodejs /app/node_modules ./node_modules

# Copy Prisma files for runtime
COPY --from=builder --chown=nextjs:nodejs /app/prisma ./prisma
COPY --from=builder --chown=nextjs:nodejs /app/prisma.config.ts ./prisma.config.ts

# Copy scripts for database setup
COPY --from=builder --chown=nextjs:nodejs /app/scripts ./scripts

# Create uploads directory with proper permissions
RUN mkdir -p /app/public/uploads/receipts /app/public/uploads/blog
RUN chown -R nextjs:nodejs /app/public/uploads

# Create logs directory for migration logs
RUN mkdir -p /app/logs
RUN chown -R nextjs:nodejs /app/logs

USER nextjs

EXPOSE 3000

ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD curl -f http://localhost:3000/api/health || exit 1

# Use tini for proper signal handling and zombie reaping
ENTRYPOINT ["tini", "--"]

# Startup command:
# 1. Run migrations using DATABASE_URL
# 2. Generate Prisma client
# 3. Start the application
CMD ["sh", "-c", "\
  echo '🚀 Starting application...' && \
  echo '📦 Running database migrations...' && \
  (./node_modules/.bin/prisma migrate deploy 2>&1 || { \
  echo '⚠️ Migration deploy failed, trying db push for existing schema...' && \
  ./node_modules/.bin/prisma db push --accept-data-loss 2>&1 || \
  echo '⚠️ DB sync also failed, continuing anyway...'; \
  }) && \
  echo '🔧 Generating Prisma client...' && \
  ./node_modules/.bin/prisma generate && \
  echo '✅ Starting Next.js server...' && \
  HOSTNAME=0.0.0.0 node server.js \
  "]

