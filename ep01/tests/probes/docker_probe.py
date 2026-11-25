"""What can a container reach, on the default network and on an --internal one? No API key, no model.
Needs a running Docker and the alpine:3.20 image already on the machine (nothing is pulled).
Run from a plain terminal: python3 tests/probes/docker_probe.py

It makes one network and one peer container with names ending in this process id, and removes both at the end."""
import os
import socket
import subprocess
import sys
import threading

IMAGE = "alpine:3.20"
NET = "agentd-probe-net-%d" % os.getpid()
PEER = "agentd-probe-peer-%d" % os.getpid()
HARDENED = ["--cap-drop", "ALL", "--read-only", "--user", "1000:1000",
            "--security-opt", "no-new-privileges", "--tmpfs", "/tmp"]
results = []


def record(name, passed, detail=""):
    results.append(passed)
    print(("ok   " if passed else "FAIL ") + name + ("" if passed or not detail else "\n       " + detail))


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=90)


def shell(command, *options):
    done = docker("run", "--rm", "--pull", "never", *options, IMAGE, "sh", "-c", command)
    if done.returncode >= 125:              # Docker itself failed: that must not count as "blocked"
        raise RuntimeError(done.stderr.strip())
    return done


def serve_hello(sock):
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        with conn:
            conn.sendall(b"hello-from-host\n")


def reaches(host, port, *options):
    done = shell("(sleep 2) | nc -w 5 %s %d" % (host, port), *options)
    return "hello" in done.stdout


def opens(host, port, *options):
    return shell("nc -w 5 %s %d </dev/null" % (host, port), *options).returncode == 0


def main():
    host = socket.socket()
    host.bind(("127.0.0.1", 0))             # loopback only: this stands for agentd
    host.listen(8)
    port = host.getsockname()[1]
    threading.Thread(target=serve_hello, args=(host,), daemon=True).start()
    try:
        made = docker("network", "create", "--internal", NET)
        if made.returncode:
            raise RuntimeError(made.stderr.strip())
        peer = docker("run", "-d", "--pull", "never", "--name", PEER, "--network", NET, IMAGE,
                      "sh", "-c", "while true; do echo hello-from-peer | nc -l -p 9000; done")
        if peer.returncode:
            raise RuntimeError(peer.stderr.strip())

        print("== default network")
        for name in ("host.docker.internal", "host.lima.internal"):
            print("     host loopback through %s: %s" % (name, "REACHED" if reaches(name, port) else "not reached"))
        print("     internet (1.1.1.1:443): %s" % ("REACHED" if opens("1.1.1.1", 443) else "not reached"))

        print("== --internal network")
        inside = ["--network", NET]
        record("a peer container is reached by name", reaches(PEER, 9000, *inside))
        for name in ("host.docker.internal", "host.lima.internal"):
            record("blocked: host loopback through %s" % name, not reaches(name, port, *inside))
        record("blocked: internet", not opens("1.1.1.1", 443, *inside))

        print("== hardened flags with --network none")
        flags = HARDENED + ["--network", "none"]
        record("a command runs and /tmp is writable", shell("echo x > /tmp/x", *flags).returncode == 0)
        record("blocked: write to /", shell("echo x > /made-here", *flags).returncode != 0)
        record("blocked: internet", not opens("1.1.1.1", 443, *flags))
    finally:
        docker("rm", "-f", PEER)
        docker("network", "rm", NET)
        host.close()
    print("%d of %d checks passed (the default network lines are reported, not counted)" % (sum(results), len(results)))
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
