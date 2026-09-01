from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer


def main() -> None:
    host = "127.0.0.1"
    port = 8000
    server = ThreadingHTTPServer((host, port), SimpleHTTPRequestHandler)
    print(f"BUFFSQUAD ACO site running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
