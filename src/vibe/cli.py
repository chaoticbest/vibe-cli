import json, os, re, shutil, subprocess, sys, time
from pathlib import Path
from typing import Optional
import typer, yaml
from rich import print
from rich.table import Table

app = typer.Typer(add_completion=False, help="Deploy vibe-coded apps to the Hub")

VIBES_ROOT = Path(os.environ.get("VIBES_ROOT", "/srv/vibes")).resolve()
STATIC_ROOT = VIBES_ROOT / "static"
APPS_ROOT = VIBES_ROOT / "apps"
REGISTRY_PATH = VIBES_ROOT / "registry" / "apps.json"

def run(cmd, cwd: Optional[Path] = None, env: Optional[dict] = None):
    print(f"[bold cyan]$[/] {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, check=True)

def slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9\-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "app"

def load_registry() -> dict:
    if REGISTRY_PATH.exists():
        return json.loads(REGISTRY_PATH.read_text())
    return {"apps": {}}

def save_registry(reg: dict):
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(reg, indent=2))
    print(f"[green]Updated registry[/] → {REGISTRY_PATH}")

def guess_output_dir(repo_dir: Path) -> Path:
    for name in ("dist", "build", "public"):
        p = repo_dir / name
        if p.exists():
            return p
    return repo_dir  # fallback

def copy_static(src: Path, dst: Path):
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    # copytree-like: copy all files/dirs except obvious config cruft
    ignore = {".git", ".github", "node_modules", ".DS_Store", "vibe.yaml"}
    for root, dirs, files in os.walk(src):
        rpath = Path(root)
        # prune ignored dirs
        dirs[:] = [d for d in dirs if d not in ignore]
        rel = rpath.relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            if f in ignore:
                continue
            shutil.copy2(rpath / f, dst / rel / f)

def parse_vibe_yaml(repo_dir: Path) -> dict:
    f = repo_dir / "vibe.yaml"
    if f.exists():
        return yaml.safe_load(f.read_text()) or {}
    return {}

@app.command()
def deploy(repo: str, app_id: Optional[str] = typer.Option(None, help="Override app id")):
    """
    Deploy a static (or prebuilt) app to /app/<id>/ by copying build output into /srv/vibes/static/<id>/
    """
    VIBES_ROOT.mkdir(parents=True, exist_ok=True)
    STATIC_ROOT.mkdir(parents=True, exist_ok=True)
    APPS_ROOT.mkdir(parents=True, exist_ok=True)

    # 1) clone or pull
    repo_url = repo
    if repo_url.endswith("/"):
        repo_url = repo_url[:-1]
    inferred = slugify(Path(repo_url).stem)
    app_id = slugify(app_id or inferred)
    work_dir = APPS_ROOT / app_id
    repo_dir = work_dir / "repo"

    if repo_dir.exists():
        print(f"[yellow]Repo exists[/] → pulling latest in {repo_dir}")
        run(["git", "-C", str(repo_dir), "pull", "--ff-only"])
    else:
        work_dir.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", repo_url, str(repo_dir)])

    # 2) config
    cfg = parse_vibe_yaml(repo_dir)
    if "id" in cfg:
        app_id = slugify(cfg["id"])

    app_type = (cfg.get("type") or "static").lower()
    build_cfg = cfg.get("build", {}) if isinstance(cfg.get("build"), dict) else {}

    # 3) optional build (static/spa)
    output_dir: Path
    if app_type in ("static", "spa"):
        install_cmd = build_cfg.get("install")
        build_cmd = build_cfg.get("command")
        base_path_env = build_cfg.get("base_path_env")
        if install_cmd:
            run(install_cmd.split(), cwd=repo_dir)
        env = os.environ.copy()
        if base_path_env:
            env[base_path_env] = f"/app/{app_id}/"
            print(f"[blue]Set {base_path_env}={env[base_path_env]}[/]")
        if build_cmd:
            run(build_cmd.split(), cwd=repo_dir, env=env)
        out = build_cfg.get("output_dir")
        output_dir = (repo_dir / out).resolve() if out else guess_output_dir(repo_dir)
    else:
        # server not supported in v1
        print("[red]This v1 only supports type=static/spa[/]")
        raise typer.Exit(code=2)

    if not output_dir.exists():
        print(f"[red]Build output not found[/]: {output_dir}")
        raise typer.Exit(code=2)

    # 4) copy into /srv/vibes/static/<id>/
    dest = STATIC_ROOT / app_id
    print(f"[green]Copying static files[/] {output_dir} → {dest}")
    copy_static(output_dir, dest)

    # 5) update registry
    reg = load_registry()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry = reg["apps"].get(app_id, {})
    created = entry.get("created_at", now)
    entry.update({
        "id": app_id,
        "name": cfg.get("name") or app_id,
        "type": app_type,
        "repo": repo_url,
        "links": {
            "app": f"https://vibes.chaoticbest.com/app/{app_id}/",
            "blog": f"https://vibes.chaoticbest.com/blog/{app_id}",
            "github": repo_url if repo_url.startswith("http") else f"https://github.com/{repo_url}"
        },
        "created_at": created,
        "updated_at": now,
        "meta": cfg.get("meta") or {}
    })
    reg["apps"][app_id] = entry
    save_registry(reg)

    print(f"\n[bold green]Deployed![/] → {entry['links']['app']}")

@app.command()
def list():
    """List registered apps."""
    reg = load_registry()
    t = Table(title="Vibe Apps")
    t.add_column("ID"); t.add_column("Type"); t.add_column("App URL"); t.add_column("Repo")
    for aid, e in sorted(reg["apps"].items()):
        t.add_row(aid, e.get("type","?"), e["links"]["app"], e.get("repo",""))
    print(t)

if __name__ == "__main__":
    app()
