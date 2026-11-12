"""Одноразовая TCP-проверка для ДЗ 4; не запускает HTTP или другие сервисы."""
import argparse
import errno
import json
import socket


def probe(host: str, port: int, timeout: float = 2) -> str:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "CONNECTED"
    except TimeoutError:
        return "TIMEOUT"
    except OSError as error:
        # REFUSED означает закрытый порт, а не доказанную фильтрацию.
        return errno.errorcode.get(error.errno, type(error).__name__)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host")
    parser.add_argument("port", type=int)
    parser.add_argument("--expect", choices=("allow", "deny"), required=True)
    args = parser.parse_args()
    result = probe(args.host, args.port)
    success = result == "CONNECTED" if args.expect == "allow" else result in {
        "TIMEOUT", "ENETUNREACH", "EHOSTUNREACH", "EACCES", "EPERM",
    }
    print(json.dumps({"host": args.host, "port": args.port, "expected": args.expect,
                      "result": result, "pass": success}))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
