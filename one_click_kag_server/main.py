"""
Automatic KAG server setup and maintenance.
"""
from pathlib import Path
import argparse
import logging
import os
import pickle
import uuid
import shutil
import subprocess
import sys

import digitalocean
import kagtcprlib.webinterface
import paramiko
from tenacity import retry, wait, wait_fixed, stop_after_delay
import tempfile
import toml
import yaml

from one_click_kag_server.ssh_keys import create_ssh_keypair
from one_click_kag_server.sftp import MySFTPClient

DEFAULT_CONFIG_PATH = Path("config.yaml")
DEFAULT_STATE_PATH = Path("state.pkl")
SSH_KEYS_DIR = Path("ssh_keys")
SSH_KEY_NAME_PREFIX = "one_click_kag_server"
FILES_TO_UPLOAD = [
    Path("droplet_setup.sh"),
    Path("docker-compose.yaml"),
    Path("Dockerfile.kag"),
]


class State:
    """
    Tracks the state of the server and the setup process.
    This class can be pickled and saved/loaded so that we have persistent state between runs.
    If this class is updated, the state file may need to be deleted.
    """
    def __init__(self):
        self.ssh_key_name = None
        self.ssh_key_uploaded = False
        self.droplet = None
        self.done_droplet_setup = False
        self.done_kag_setup = False

    def save(self, path: Path = DEFAULT_STATE_PATH):
        logging.info("Saving state to %s", path)
        with open(path, "wb") as fh:
            pickle.dump(self, fh)

    @staticmethod
    def load(path: Path = DEFAULT_STATE_PATH) -> "State":
        if path.exists():
            logging.info("Loading state from %s", path)
            with open(path, "rb") as fh:
                return pickle.load(fh)
        logging.info("Creating a fresh state")
        return State()


def create_droplet(config: dict, state: State):
    """Create the droplet in DigitalOcean."""
    logging.info("Creating droplet...")
    if not state.ssh_key_name:
        raise RuntimeError("No ssh key configured yet")

    manager = digitalocean.Manager(token=config["secrets"]["digitalocean_key"])
    keys = manager.get_all_sshkeys()
    keys_to_use = []
    for key in keys:
        if key.name == state.ssh_key_name:
            keys_to_use.append(key)

    droplet = digitalocean.Droplet(
        token=config["secrets"]["digitalocean_key"],
        ssh_keys=keys_to_use,
        **config["droplet"]
    )
    droplet.create()
    state.droplet = droplet


@retry(wait=wait_fixed(5), stop=stop_after_delay(120))
def wait_for_droplet_to_be_active(state: State):
    """
    Block until the droplet is active and ready to be SSH'd to.
    Will time out if this takes too long.
    """
    logging.info("Waiting for droplet to be active...")
    droplet = state.droplet
    droplet.load()
    if droplet.status != "active":
        raise ValueError("Droplet is not active yet")

    ssh = open_ssh_connection(state)
    (_, stdout, _) = ssh.exec_command("echo hello world")
    stdout.channel.recv_exit_status()


def load_config_yaml(path: str) -> dict:
    """Load the config file."""
    logging.info("Loading config from %s", path)
    with open(path, "r") as fh:
        return yaml.load(fh, Loader=yaml.FullLoader)


def check_config(config: dict):
    """Check that the config is valid."""
    if not config.get("secrets", {}).get("digitalocean_key"):
        raise ValueError("Config is missing secrets.digitalocean_key")

    for mod in config.get("kag", {}).get("mods"):
        if not Path(f"Mods/{mod}").is_dir():
            raise ValueError(f"Mod {mod} is listed in config but was not found in the Mods/ directory.")


def configure_ssh_key(config: dict, state: State):
    """Create an SSH keypair, save it locally and upload it to DigitalOcean."""
    if not SSH_KEYS_DIR.exists():
        SSH_KEYS_DIR.mkdir()

    state.ssh_key_name = f"{SSH_KEY_NAME_PREFIX}_{get_unique_id()}"

    logging.info("Creating ssh keypair in %s", SSH_KEYS_DIR / state.ssh_key_name)
    (prv_key, pub_key) = create_ssh_keypair()
    with open(SSH_KEYS_DIR / state.ssh_key_name, "wb") as fh:
        fh.write(prv_key)
    with open(SSH_KEYS_DIR / f"{state.ssh_key_name}.pub", "wb") as fh:
        fh.write(pub_key)

    logging.info("Uploading key %s to DigitalOcean", state.ssh_key_name)
    key = digitalocean.SSHKey(token=config["secrets"]["digitalocean_key"],
                              name=state.ssh_key_name,
                              public_key=pub_key.decode())
    key.create()
    logging.info("Key %s uploaded successfully", state.ssh_key_name)
    state.ssh_key_uploaded = True


def get_unique_id() -> str:
    """Get a random unique identifier."""
    return str(uuid.uuid4()).split("-")[0]


def open_ssh_connection(state: State) -> paramiko.SSHClient:
    """Create an SSHClient that can be used to run commands on the server."""
    key_filename = str(SSH_KEYS_DIR / state.ssh_key_name)
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    pkey = paramiko.RSAKey.from_private_key_file(key_filename)
    ssh.connect(state.droplet.ip_address, username="root", pkey=pkey)
    return ssh


def setup_droplet(config: dict, state: State):
    """Setup the droplet ready to run KAG."""
    logging.info("Setting up droplet...")
    if not state.droplet or state.droplet.status != "active":
        raise RuntimeError("Droplet isn't active yet")

    # Upload the setup script and other required files
    ssh = open_ssh_connection(state)
    sftp = ssh.open_sftp()
    for path in FILES_TO_UPLOAD:
        logging.info("Uploading file %s", path)
        sftp.put(path, path.name)

    # Run the setup script
    (_, stdout, _) = ssh.exec_command("chmod +x droplet_setup.sh")
    stdout.channel.recv_exit_status()

    (_, stdout, _) = ssh.exec_command("./droplet_setup.sh 2>&1")
    for line in stdout:
        print(line.rstrip())
    status = stdout.channel.recv_exit_status()

    sftp.close()
    ssh.close()

    if status != 0:
        raise RuntimeError("Droplet setup failed")

    state.done_droplet_setup = True


def setup_kag(config: dict, state: State):
    """
    Setup KAG, once the droplet has been properly configured.
    This can also be used to restart the KAG server.
    """
    logging.info("Setting up KAG...")

    ssh = open_ssh_connection(state)
    sftp = MySFTPClient.from_transport(ssh.get_transport())

    logging.info("Uploading docker-compose.yaml...")
    sftp.put("docker-compose.yaml", "docker-compose.yaml")

    # Upload mods
    logging.info("Uploading mods...")
    sftp.mkdir("Mods", ignore_existing=True)
    sftp.put_dir("Mods", "Mods")

    # Upload cache
    logging.info("Uploading cache...")
    sftp.mkdir("Cache", ignore_existing=True)
    sftp.put_dir("Cache", "Cache")

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_dir = Path(tmp_dir)

        logging.info("Creating autoconfig.cfg...")
        with open(tmp_dir / "autoconfig.cfg", "w") as fh:
            for key, value in config["kag"]["autoconfig"].items():
                fh.write(f"{key} = {value}\n")

        logging.info("Creating mods.cfg...")
        with open(tmp_dir / "mods.cfg", "w") as fh:
            for mod in config["kag"]["mods"]:
                fh.write(f"{mod}\n")

        logging.info("Creating security files...")
        tmp_security_dir = tmp_dir / "Security"
        tmp_security_dir.mkdir()
        shutil.copyfile(Path("Security") / "seclevs.cfg", tmp_security_dir / "seclevs.cfg")
        shutil.copyfile(Path("Security") / "normal.cfg", tmp_security_dir / "normal.cfg")

        with open(Path("Security") / "superadmin.cfg", "r") as fh:
            superadmin_cfg = fh.read().replace("$USERS", "; ".join(config["kag"]["security"]["superadmins"]))
        with open(tmp_security_dir / "superadmin.cfg", "w") as fh:
            fh.write(superadmin_cfg)

        with open(Path("Security") / "admin.cfg", "r") as fh:
            admin_cfg = fh.read().replace("$USERS", "; ".join(config["kag"]["security"]["admins"]))
        with open(tmp_security_dir / "admin.cfg", "w") as fh:
            fh.write(admin_cfg)

        logging.info("Uploading autoconfig.cfg, mods.cfg, security files...")
        sftp.put(str(Path(tmp_dir) / "autoconfig.cfg"), "autoconfig.cfg")
        sftp.put(str(Path(tmp_dir) / "mods.cfg"), "mods.cfg")
        sftp.mkdir("Security", ignore_existing=True)
        sftp.put_dir(str(tmp_security_dir), "Security")

    (_, stdout, _) = ssh.exec_command("docker-compose down 2>&1")
    for line in stdout:
        print(line.rstrip())
    stdout.channel.recv_exit_status()

    (_, stdout, _) = ssh.exec_command("docker-compose up -d 2>&1")
    for line in stdout:
        print(line.rstrip())
    status = stdout.channel.recv_exit_status()
    if status != 0:
        raise RuntimeError("Failed to setup KAG")
    ssh.close()

    state.done_kag_setup = True


def follow_kag_logs(state: State):
    """Show the logs from the KAG server in real-time."""
    logging.info("Showing KAG logs...")
    ssh = open_ssh_connection(state)
    (_, stdout, _) = ssh.exec_command("docker-compose logs -f kag 2>&1")
    for line in stdout:
        print(line.rstrip())


def run_command_up(config: dict, state: State):
    """Create the droplet, configure it to run KAG and start the KAG server."""
    if not state.ssh_key_uploaded:
        configure_ssh_key(config, state)

    if state.droplet:
        logging.info("Droplet already exists, will not create a new one.")
    else:
        create_droplet(config, state)
        wait_for_droplet_to_be_active(state)

    state.droplet.load()
    logging.info("Droplet info: %s, ip=%s, status=%s", state.droplet, state.droplet.ip_address, state.droplet.status)

    if state.done_droplet_setup:
        logging.info("Droplet setup already done.")
    else:
        setup_droplet(config, state)

    if state.done_kag_setup:
        logging.info("KAG setup already done.")
    else:
        setup_kag(config, state)


def run_command_down(config: dict, state: State):
    """Destroy the droplet & save cache."""

    ssh = open_ssh_connection(state)
    sftp = MySFTPClient.from_transport(ssh.get_transport())

    logging.info("Saving cache")
    sftp.get_recursive("Cache", "Cache")

    logging.info("Destroying droplet...")
    state.droplet.destroy()


def exec_ssh(state: State):
    """Start an SSH session to the server. Requires ssh to be installed locally."""
    logging.info("Starting SSH session...")
    subprocess.check_call(["ssh", "-i", str(SSH_KEYS_DIR / state.ssh_key_name), f"root@{state.droplet.ip_address}"],
                          stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)


def run_command_rcon(config: dict, state: State):
    """Use kagtcprlib to open a web based RCON session."""
    if config["kag"]["autoconfig"].get("sv_tcpr") != 1:
        raise RuntimeError("sv_tcpr is not set in autoconfig. Set it to 1 to use RCON.")
    if not config["kag"]["autoconfig"].get("sv_rconpassword"):
        raise RuntimeError("sv_rconpassword is not set in autoconfig. Set it to use RCON.")

    kagtcprlib_cfg = {
        "my-kag-server":
            {
                "host": state.droplet.ip_address,
                "port": 50301,
                "rcon_password": config["kag"]["autoconfig"]["sv_rconpassword"],
            }
        }
    with tempfile.TemporaryDirectory() as tmp_dir:
        with open(Path(tmp_dir) / "config.toml", "w") as fh:
            toml.dump(kagtcprlib_cfg, fh)
        kagtcprlib.webinterface.run(str(Path(tmp_dir) / "config.toml"))


def main():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["up", "down", "kag-logs", "restart-kag", "ssh", "rcon"])
    parser.add_argument("--config-file", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to configuration file.")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_PATH, help="Path to configuration file.")
    args = parser.parse_args()

    if not Path(args.config_file).exists():
        raise RuntimeError(f"Config file {args.config_file} does not exist")

    cwd = os.getcwd()
    config = load_config_yaml(path=args.config_file)
    state = State.load(path=args.state_file)
    state.done_kag_setup = False
    check_config(config)

    try:
        if args.command == "up":
            run_command_up(config, state)
            logging.info("DONE. Server running at %s", state.droplet.ip_address)
        elif args.command == "down":
            run_command_down(config, state)
            state = State()
            logging.info("DONE. Droplet destroyed.")
        elif args.command == "restart-kag":
            setup_kag(config, state)
            logging.info("DONE. Restarted KAG.")
        elif args.command == "kag-logs":
            follow_kag_logs(state)
        elif args.command == "ssh":
            exec_ssh(state)
        elif args.command == "rcon":
            run_command_rcon(config, state)
        else:
            raise RuntimeError(f"Unrecognised command {args.command}")
    except:
        raise
    finally:
        os.chdir(cwd)  # incase we've changed directory, e.g. with the web server for the rcon command
        state.save(path=args.state_file)


if __name__ == "__main__":
    main()
