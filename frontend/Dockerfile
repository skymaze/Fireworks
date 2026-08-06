FROM node:22-alpine AS build
WORKDIR /app
# 用提交的 lock 文件 + npm ci，保证与仓库一致的依赖解析（而非按 ^ 重解析）
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
RUN npm run build

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app/.output ./.output
EXPOSE 3000
CMD ["node", ".output/server/index.mjs"]
