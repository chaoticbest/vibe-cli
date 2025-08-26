import json, os, re, shutil, subprocess, sys, time
from pathlib import Path
from typing import Optional
import typer, yaml
from rich import print
from rich.table import Table
from textwrap import dedent

app = typer.Typer(add_completion=False, help="Deploy vibe-coded apps to the Hub")

VIBES_ROOT = Path(os.environ.get("VIBES_ROOT", "/srv/vibes")).resolve()
STATIC_ROOT = VIBES_ROOT / "static"
APPS_ROOT = VIBES_ROOT / "apps"
REGISTRY_PATH = VIBES_ROOT / "registry" / "apps.json"

def make_traefik_labels(app_id: str, internal_port: int) -> list[str]:
    rid = slugify(app_id).replace("-", "_")
    host = "vibes.chaoticbest.com"
    return [
        "traefik.enable=true",
        f"traefik.http.routers.{rid}.rule=Host(`{host}`) && PathPrefix(`/app/{app_id}`)",
        "traefik.http.routers.{rid}.entrypoints=web,websecure".format(rid=rid),
        "traefik.http.routers.{rid}.tls=true".format(rid=rid),
        "traefik.http.routers.{rid}.tls.certresolver=le".format(rid=rid),
        "traefik.http.routers.{rid}.priority=100".format(rid=rid),
        f"traefik.http.middlewares.{rid}-strip.stripprefix.prefixes=/app/{app_id}",
        f"traefik.http.routers.{rid}.middlewares={rid}-strip",
        f"traefik.http.services.{rid}.loadbalancer.server.port={internal_port}",
    ]

def generate_dockerfile(repo_dir: Path, app_id: str, server_cfg: dict) -> Path:
    """Create a minimal Dockerfile if repo doesn't provide one."""
    runtime = (server_cfg.get("runtime") or "").lower()
    install = server_cfg.get("install")
    start = server_cfg.get("start")
    port = int(server_cfg.get("port", 3000))
    ddir = APPS_ROOT / slugify(app_id) / ".deploy"
    ddir.mkdir(parents=True, exist_ok=True)
    out = ddir / "Dockerfile.generated"

    if runtime == "node":
        content = f"""
        FROM node:20-alpine
        WORKDIR /app
        COPY package*.json ./
        {'RUN ' + install if install else 'RUN npm ci --omit=dev'}
        COPY . .
        ENV PORT={port}
        EXPOSE {port}
        CMD ["sh","-lc","{start or 'npm start'}"]
        """
    elif runtime == "python":
        content = f"""
        FROM python:3.11-slim
        WORKDIR /app
        COPY requirements*.txt ./
        {'RUN ' + install if install else 'RUN pip install --no-cache-dir -r requirements.txt || true'}
        COPY . .
        ENV PORT={port}
        EXPOSE {port}
        CMD ["sh","-lc","{start or f'uvicorn app:app --host 0.0.0.0 --port {port}'}"]
        """
    else:
        raise RuntimeError("runtime must be 'node' or 'python' (or provide server.dockerfile)")

    out.write_text(dedent(content).strip() + "\n")
    return out

def write_compose(app_id: str, repo_dir: Path, server_cfg: dict) -> Path:
    """Emit docker-compose.yml for this app."""
    sid = slugify(app_id)
    ddir = APPS_ROOT / sid / ".deploy"
    ddir.mkdir(parents=True, exist_ok=True)

    port = int(server_cfg.get("port", 3000))
    dockerfile = server_cfg.get("dockerfile")
    labels = make_traefik_labels(sid, port)
    env_file = server_cfg.get("env_file")
    env_names = server_cfg.get("env") or []

    # Environment entries (from current shell) + PORT
    env_map = {"PORT": str(port)}
    for name in env_names:
        if name in os.environ:
            env_map[name] = os.environ[name]

    # If Dockerfile not in repo, generate one
    df_path: Path
    if dockerfile:
        df_path = (repo_dir / dockerfile).resolve()
        if not df_path.exists():
            raise FileNotFoundError(f"server.dockerfile not found: {df_path}")
    else:
        df_path = generate_dockerfile(repo_dir, sid, server_cfg)

    # Compose will build from repo root with specified Dockerfile
    compose_yaml = {
        "version": "3.9",
        "services": {
            "app": {
                "build": {
                    "context": str(repo_dir),
                    "dockerfile": str(df_path),
                },
                # optional tag makes rebuilds faster if reused
                "image": f"vibe-{sid}:latest",
                "restart": "unless-stopped",
                "networks": ["vibes_net"],
                "labels": labels,
            }
        },
        "networks": {"vibes_net": {"external": True}},
    }
    svc = compose_yaml["services"]["app"]
    if env_map:
        svc["environment"] = env_map
    if env_file:
        svc["env_file"] = [env_file]

    yml_path = ddir / "docker-compose.yml"
    yml_path.write_text(yaml.safe_dump(compose_yaml, sort_keys=False))
    return yml_path


def safe_rmtree(path: Path) -> bool:
    try:
        if path.exists():
            if path.is_file() or path.is_symlink():
                path.unlink()
            else:
                shutil.rmtree(path)
            return True
        return False
    except Exception as e:
        print(f"[red]Failed to remove[/] {path}: {e}")
        return False

def compose_down_if_present(app_id: str) -> bool:
    """Stop a dynamic app if a compose file exists (future-proof)."""
    deploy_dir = APPS_ROOT / app_id / ".deploy"
    yml = deploy_dir / "docker-compose.yml"
    if yml.exists():
        print(f"[cyan]Bringing down compose stack[/] in {deploy_dir}")
        run(["docker", "compose", "-f", str(yml), "down", "--remove-orphans", "-v"])
        return True
    return False

def docker_run(image: str, workdir: Path, commands: str, env: dict):
    cmd = ["docker","run","--rm","-v",f"{workdir}:/src","-w","/src"]
    for k,v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [image, "sh", "-lc", commands]
    run(cmd) 

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
    use_docker = bool(build_cfg.get("use_docker"))
    docker_image = build_cfg.get("image", "node:20-alpine")

    if app_type in ("static","spa"):
        install_cmd = build_cfg.get("install")
        build_cmd   = build_cfg.get("command")
        base_path_env = build_cfg.get("base_path_env")

        env = os.environ.copy()
        if base_path_env:
            env[base_path_env] = f"/app/{app_id}/"
            print(f"[blue]Set {base_path_env}={env[base_path_env]}[/]")

        # env vars from shell + env_file
        for name in (build_cfg.get("env") or []):
            if name in os.environ: env[name] = os.environ[name]
        env_file = build_cfg.get("env_file")
        if env_file and Path(env_file).exists():
            for line in Path(env_file).read_text().splitlines():
                line=line.strip()
                if not line or line.startswith("#") or "=" not in line: continue
                k,v = line.split("=",1); env[k.strip()] = v.strip()
            print(f"[green]Loaded build env from[/] {env_file}")

        if use_docker:
            cmds = []
            if install_cmd: cmds.append(install_cmd)
            if build_cmd:   cmds.append(build_cmd)
            if not cmds:    cmds.append("npm ci && npm run build")
            docker_run(docker_image, repo_dir, " && ".join(cmds), env)
        else:
            if install_cmd: run(install_cmd.split(), cwd=repo_dir)
            if build_cmd:   run(build_cmd.split(),   cwd=repo_dir, env=env)

        out = build_cfg.get("output_dir")
        output_dir = (repo_dir / out).resolve() if out else guess_output_dir(repo_dir)
    elif app_type == "server":
        server_cfg = cfg.get("server") or {}
        # Compose file for this app
        yml = write_compose(app_id, repo_dir, server_cfg)
        print(f"[green]Compose written[/] → {yml}")
        # Bring it up (build image + start)
        run(["docker", "compose", "-f", str(yml), "up", "-d", "--build"])
        output_dir = None  # not used
    else:
        print("[red]Unknown app type[/]:", app_type)
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
def undeploy(
    app_id: str = typer.Argument(..., help="App id to undeploy (slug)"),
    purge: bool = typer.Option(False, help="Also delete cloned repo/work dir"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """
    Remove an app from the Hub:
      - delete /srv/vibes/static/<id>/
      - docker compose down (if present)
      - remove registry entry
      - optionally delete /srv/vibes/apps/<id>/ with --purge
    """
    sid = slugify(app_id)
    reg = load_registry()
    exists_in_registry = sid in reg.get("apps", {})

    # Show what will happen
    static_dir = STATIC_ROOT / sid
    work_dir = APPS_ROOT / sid
    blog_md = VIBES_ROOT / "blog" / f"{sid}.md"
    actions = [
        f"Stop dynamic stack (if any) at {work_dir}/.deploy/",
        f"Remove static files at {static_dir}",
        f"Remove registry entry for '{sid}'" + ("" if exists_in_registry else " (not found; will skip)"),
    ]
    if purge:
        actions.append(f"Delete work dir at {work_dir}")
    # (optional) remove blog stub if you use per-app markdown names
    if blog_md.exists():
        actions.append(f"Remove blog markdown at {blog_md}")

    print("[bold]Planned actions:[/]")
    for a in actions:
        print(f"  • {a}")

    if not yes:
        if not typer.confirm(f"Proceed to undeploy '{sid}'?", default=False):
            print("[yellow]Aborted.[/]")
            raise typer.Exit(code=1)

    # 1) bring down any compose stack
    try:
        compose_down_if_present(sid)
    except subprocess.CalledProcessError as e:
        print(f"[red]compose down failed[/]: {e}")

    # 2) remove static files
    if safe_rmtree(static_dir):
        print(f"[green]Removed[/] {static_dir}")
    else:
        print(f"[yellow]Static dir not found[/]: {static_dir}")

    # 3) remove blog stub if present (optional)
    if blog_md.exists():
        if safe_rmtree(blog_md):
            print(f"[green]Removed[/] {blog_md}")

    # 4) remove from registry
    if exists_in_registry:
        del reg["apps"][sid]
        save_registry(reg)
        print(f"[green]Registry entry removed[/]: {sid}")
    else:
        print(f"[yellow]No registry entry for[/] {sid}")

    # 5) optionally remove cloned repo/work dir
    if purge:
        if safe_rmtree(work_dir):
            print(f"[green]Removed work dir[/]: {work_dir}")
        else:
            print(f"[yellow]Work dir not found[/]: {work_dir}")

    print(f"\n[bold green]Undeployed '{sid}'.[/]")


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
