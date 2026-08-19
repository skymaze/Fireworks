# Node 24（当前 active LTS，支持至 2028-04；22 已进 maintenance）
#
# build stage 固定在构建宿主原生架构（BUILDPLATFORM）上执行 npm 安装与构建：
# 产物 .output 是平台无关的纯 JS，两个目标架构（amd64/arm64）的产物镜像都
# 复用同一份产物，避免在 QEMU 模拟的 arm64 中跑 npm（此前出现 qemu SIGILL
# 崩溃后挂死，拖满 90min 构建超时）。
FROM --platform=$BUILDPLATFORM node:24-alpine AS build
WORKDIR /app
# 用提交的 lock 文件 + npm ci，保证与仓库一致的依赖解析（而非按 ^ 重解析）
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund --fetch-timeout=120000 --fetch-retries=3
COPY . .
RUN npm run build

FROM node:24-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY --from=build /app/.output ./.output
EXPOSE 3000
CMD ["node", ".output/server/index.mjs"]
