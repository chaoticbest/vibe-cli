# vibe-cli (v0.1)

Deploy static/spa apps to vibes.chaoticbest.com.

## Install from source (on the Hub)

```bash
sudo apt-get update && sudo apt-get -y install python3-venv git
git clone https://github.com/chaoticbest/vibe-cli.git
cd vibe-cli
python3 -m venv .venv && source .venv/bin/activate
pip install -U pip && pip install -e .
```

## Usage

```bash
vibe deploy https://github.com/chaoticbest/hello.git
vibe list
```

---

# B) Install/build the CLI (on your EC2 Hub)

```bash
sudo apt-get -y install python3-venv git
cd /srv
git clone https://github.com/chaoticbest/vibe-cli.git
cd vibe-cli
python3 -m venv .venv && source .venv/bin/activate
pip install -U pip && pip install -e .
hash -r  # refresh shell's idea of PATH if needed
which vibe
```

> If which vibe doesn’t show a path, open a new shell (or source ~/.bashrc).
