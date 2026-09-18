# DataSage 前端镜像：Vite dev server（容器内通过 VITE_PROXY_TARGET 代理后端）
FROM node:20-alpine

WORKDIR /app

# 先装依赖（利用 Docker 层缓存；package-lock 存在时 npm ci 更严格可复现）
COPY package.json package-lock.json* ./
RUN npm install

# 再拷源码
COPY . .

EXPOSE 5173

# --host 让容器外可访问；代理目标由 compose 注入 VITE_PROXY_TARGET
CMD ["npm", "run", "dev", "--", "--host"]
