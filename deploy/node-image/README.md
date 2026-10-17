# 3x-ui + KIT image

`build.sh` pins the project fork of 3x-ui to commit
`95f19b192f477b59cc368dcb7751bcf2e0180e5b` and checks the SHA-256 of
KIT v1.2 `kit-sub.py` before building. The local `scripts/kit.sh` is packaged at
`/opt/kit/kit-host.sh` for inspection. It needs systemd and must only be run on
a host, never inside this container. No code is downloaded when the image starts.

```bash
deploy/node-image/build.sh
docker image inspect freedomvpn/3x-ui-kit:95f19b1-kit1.2
```

The image runs the project's 3x-ui fork, including master/child support. Copy
`compose.example.yml` to the new node and mount persistent `db`, `cert`, `acme`
and `logs` directories. Start the panel with `docker compose up -d xui`. Configure
its credentials, TLS and API token, then register it as a child of the master.
Clone approved inbounds from the master; the image deliberately contains no user
keys, advertising links, camouflage domains or pre-created connections.

The current build is also exported on the master for transfer to a new host:

```bash
scp root@MASTER:/root/freedomvpn-3x-ui-kit-95f19b1-kit1.2.tar.gz .
sha256sum freedomvpn-3x-ui-kit-95f19b1-kit1.2.tar.gz
gunzip -c freedomvpn-3x-ui-kit-95f19b1-kit1.2.tar.gz | docker load
```

The expected archive SHA-256 is
`f9272bd4ef44721fb0a22d1e81be9fd9178002d20e5bf45fc8446c4aa8beb48b`.

For KIT's application-aware subscription, create a node-specific
`kit-sub/config.json` with `path`, `listen`, `port`, `upstream` and `host`, then
start `docker compose --profile subscription up -d`. Set `upstream` to the local
3x-ui subscription listener. Mount a valid certificate/key when serving TLS from
KIT directly. Restrict the 3x-ui panel and subscription listener by firewall.
The KIT subscription process is a separate container so it can be restarted
independently of Xray.

This container setup does not execute the upstream KIT host installer. Its
systemd, nginx, firewall and package-manager steps are not compatible with a
normal Docker container. The existing master/child control plane creates and
syncs protocol inbounds for new nodes.
