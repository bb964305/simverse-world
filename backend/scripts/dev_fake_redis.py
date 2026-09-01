"""Run a disposable Redis-compatible TCP server for local development only."""

from fakeredis import TcpFakeServer


def main() -> None:
    server = TcpFakeServer(("127.0.0.1", 6379))
    print("Disposable fakeredis listening on redis://127.0.0.1:6379/0", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
