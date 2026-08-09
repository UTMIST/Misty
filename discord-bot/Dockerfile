FROM node:22-slim

WORKDIR /app

# Install production dependencies against the lockfile for reproducibility.
# ffmpeg decode/mix and the Opus/ffmpeg peer stack live in the meeting service
# now (see services/meeting), not the bot, so no apt packages or peer-dep
# overrides are needed here.
# .npmrc carries engine-strict=true, so it has to land BEFORE npm ci — copied
# later (with the source) the engines check is skipped for the image build.
# With it here, a base tag that ever drifts below the >=22.12 floor fails the
# build instead of quietly shipping a runtime @discordjs/voice doesn't support.
COPY package.json package-lock.json .npmrc ./
RUN npm ci --omit=dev

# Copy the application source (node_modules/.env excluded via .dockerignore).
COPY . .

ENV NODE_ENV=production

# Env vars (DISCORD_TOKEN, DIRECTORY_BASE_URL, DIRECTORY_API_KEY, ENABLE_*) are
# injected by the hosting platform. The container does NOT read a .env file, so
# we invoke node directly rather than `npm start` (which passes --env-file=.env).
CMD ["node", "src/index.js"]
