FROM node:22-alpine

ENV NODE_ENV=production
WORKDIR /opt/freedomvpn
COPY assistant-mcp.mjs ./assistant-mcp.mjs
USER 1000:1000
EXPOSE 8080
CMD ["node", "assistant-mcp.mjs"]
