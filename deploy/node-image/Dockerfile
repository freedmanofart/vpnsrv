ARG XUI_BASE_IMAGE=freedomvpn/3x-ui-fork:95f19b1
FROM ${XUI_BASE_IMAGE}

RUN apk add --no-cache python3 py3-yaml jq

COPY deploy/node-image/kit-sub.py /opt/kit/kit-sub.py
COPY scripts/kit.sh /opt/kit/kit-host.sh

RUN python3 -m py_compile /opt/kit/kit-sub.py \
    && bash -n /opt/kit/kit-host.sh \
    && chmod 0755 /opt/kit/kit-sub.py /opt/kit/kit-host.sh

LABEL org.opencontainers.image.title="Freedom VPN 3x-ui with KIT" \
      org.opencontainers.image.description="3x-ui child panel with pinned KIT subscription service"
