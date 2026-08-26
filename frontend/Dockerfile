FROM node:20-alpine AS base
WORKDIR /app

COPY package.json ./
RUN npm install

COPY . .

FROM base AS build
ARG DASS_BACKEND_URL=http://api-server:8000
ENV NODE_ENV=production
ENV DASS_BACKEND_URL=$DASS_BACKEND_URL
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3000
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
EXPOSE 3000
CMD ["node", "server.js"]
