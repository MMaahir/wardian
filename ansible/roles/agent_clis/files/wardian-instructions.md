# Wardian sandbox

You are in a Wardian sandbox: a disposable Ubuntu VM that holds one project, in /home/agent/project. You run as the unprivileged user `agent`.

- Install system packages with `sudo wardian-apt <package>...`. It takes package names from Ubuntu's archive and installs only new packages. It refuses options, paths, `.deb` files and anything that would upgrade or remove an installed package. Other `sudo` commands don't work.
- Install everything else into your home directory with user-space installers, such as uv, pip, npm, mise or rustup.
- Commit only on `agent/<task>` branches. The developer fetches only those.
- Docker and other container runtimes are unavailable.
- The public internet works over IPv4. Private and local network addresses, and all IPv6 except loopback, are blocked.
