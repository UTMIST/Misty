FROM node:22-slim

WORKDIR /app

# Install production dependencies against the lockfile for reproducibility.
# ffmpeg decode/mix and the Opus/ffmpeg peer stack live in the meeting service
# now (see services/meeting), not the bot, so no apt packages or peer-dep
# overrides are needed here.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

# Copy the application source (node_modules/.env excluded via .dockerignore).
COPY . .

ENV NODE_ENV=production

# Env vars (DISCORD_TOKEN, DIRECTORY_BASE_URL, DIRECTORY_API_KEY, ENABLE_*) are
# injected by the hosting platform. The container does NOT read a .env file, so
# we invoke node directly rather than `npm start` (which passes --env-file=.env).
CMD ["node", "src/index.js"]
