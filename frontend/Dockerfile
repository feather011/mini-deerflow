# ============================================================
# 前端生产镜像（Phase 23）：两阶段构建 + nginx 轻量静态服务
#
# - 构建阶段：npm ci 装锁定依赖 → vite build 产出静态资源
# - 运行阶段：nginx 只放静态文件，/api 同源反代到后端
# - 最终镜像不含 node_modules / 源码 / .env
# - 非 root 用户运行（8080 为非特权端口）
# ============================================================

# ---- 构建阶段 ----
FROM node:24-alpine AS build

WORKDIR /app

# 先拷锁文件装依赖：源码改动不会让这一层重新下载
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# 同源反代：**base 必须归一成空串**，让请求打到相对路径 /api/...，
# 由 nginx 反代给后端——生产环境因此不需要 CORS（开发仍是跨源，行为不变）。
#
# 写成 '/' 而不是 '/api'：调用方传的路径本身已带 /api 前缀
# （apiGet('/api/health')），base 再给 '/api' 会拼成 /api/api/health
# → 后端无此路由 → 全部 404 → 页面白屏（实测踩过）。
# 写 '/' 而不是空串：空的环境变量存在被 Vite 丢弃的风险，一旦变成
# undefined 就会回退到 127.0.0.1:8000，在生产里变成跨源请求。
RUN VITE_API_BASE_URL=/ npm run build

# ---- 运行阶段 ----
FROM nginx:alpine

# 非 root 运行的两个坑（都已实测踩过）：
#   1) pid 默认写 /run/nginx.pid（root 属主）→ 必须改到 /tmp。
#      用正则匹配整条 pid 语句，而不是写死 /var/run/nginx.pid ——
#      当前镜像里就是 /run/nginx.pid，写死的替换会静默失效。
#   2) 不 chown /var/log/nginx：那里是指向 stdout/stderr 的符号链接，
#      chown 会作用到链接目标上，可能反而破坏日志输出。
RUN rm -f /etc/nginx/conf.d/default.conf \
    && sed -i -E 's#^\s*pid\s+[^;]+;#pid /tmp/nginx.pid;#' /etc/nginx/nginx.conf \
    && chown -R nginx:nginx /var/cache/nginx

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

USER nginx
EXPOSE 8080

CMD ["nginx", "-g", "daemon off;"]
