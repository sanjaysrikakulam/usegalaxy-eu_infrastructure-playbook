#!/usr/bin/env python
# Description: Galaxy jobs's job working directory (JWD) script. Can get you
# the path of a JWD and can delete JWD's of job failed within last X days.

import argparse
import os
import shutil
import sys
from datetime import datetime
from xml.dom.minidom import parse
import psycopg2
import yaml


def main():
    """
    JWD script
    1. Can get you the path of a JWD
    2. Can delete JWD's of job failed within last X days
    """
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(
        dest="subcommand",
        title="""
        Use one of the following subcommands:
            get_jwd: Get JWD path of a given Galaxy job id
            clean_jwds: Clean JWD's of jobs failed within last X days

        The following ENVs (same as gxadmin's) should be set:
            GALAXY_CONFIG_FILE: Path to the galaxy.yml file
            GALAXY_LOG_DIR: Path to the Galaxy log directory
            PGDATABASE: Name of the Galaxy database
            PGUSER: Galaxy database user
            PGHOST: Galaxy database host
        We also need a ~/.pgpass file (same as gxadmin's) in format:
            <pg_host>:5432:*:<pg_user>:<pg_password>

        Example:
            get_jwd:
                python galaxy_jwd.py get_jwd 12345678
            clean_jwds:
                Dry run: python galaxy_jwd.py clean_jwds --dry_run --days 5
                No dry run: python galaxy_jwd.py clean_jwds --no_dry_run --days 5
        """,
    )

    # Parser for the get_jwd subcommand
    get_jwd_parser = subparsers.add_parser(
        "get_jwd", help="Get JWD path of a given Galaxy job id"
    )
    get_jwd_parser.add_argument(
        "job_id",
        help="Galaxy job id",
    )

    # Parser for the clean_jwds subcommand
    clean_jwds_parser = subparsers.add_parser(
        "clean_jwds", help="Clean JWD's of jobs failed within last X days"
    )
    dry_run_group = clean_jwds_parser.add_mutually_exclusive_group(required=True)
    dry_run_group.add_argument(
        "--dry_run",
        help="Dry run (Print's the JWD's that would be deleted)",
        action="store_true",
    )
    dry_run_group.add_argument(
        "--no_dry_run",
        help="No dry run (Deletes the JWD's)",
        action="store_true",
    )
    clean_jwds_parser.add_argument(
        "--days",
        help="Number of days within which the jobs were last updated to be considered for deletion (default: 5)",
        default=5,
    )

    args = parser.parse_args(args=None if sys.argv[1:] else ["--help"])

    # Check if environment variables are set
    if not os.environ.get("GALAXY_CONFIG_FILE"):
        raise ValueError("Please set ENV GALAXY_CONFIG_FILE")
    galaxy_config_file = os.environ.get("GALAXY_CONFIG_FILE").strip()

    # Check if the given galaxy.yml file exists
    if not os.path.isfile(galaxy_config_file):
        raise ValueError(
            f"The given galaxy.yml file {galaxy_config_file} does not exist"
        )

    # pulsar_app.yml file path for pulsar_embedded runner
    if not os.environ.get("GALAXY_PULSAR_APP_CONF"):
        raise ValueError("Please set ENV GALAXY_PULSAR_APP_CONF")
    galaxy_pulsar_app_conf = os.environ.get("GALAXY_PULSAR_APP_CONF").strip()

    if not os.environ.get("GALAXY_LOG_DIR"):
        raise ValueError("Please set ENV GALAXY_LOG_DIR")
    galaxy_log_dir = os.environ.get("GALAXY_LOG_DIR").strip()

    if not os.environ.get("PGDATABASE"):
        raise ValueError("Please set ENV PGDATABASE")
    db_name = os.environ.get("PGDATABASE").strip()

    if not os.environ.get("PGUSER"):
        raise ValueError("Please set ENV PGUSER")
    db_user = os.environ.get("PGUSER").strip()

    if not os.environ.get("PGHOST"):
        raise ValueError("Please set ENV PGHOST")
    db_host = os.environ.get("PGHOST").strip()

    # Check if ~/.pgpass file exists and is not empty
    if (
        not os.path.isfile(os.path.expanduser("~/.pgpass"))
        or os.stat(os.path.expanduser("~/.pgpass")).st_size == 0
    ):
        raise ValueError(
            "Please create a ~/.pgpass file in format: <pg_host>:5432:*:<pg_user>:<pg_password>"
        )
    db_password = extract_password_from_pgpass(
        pgpass_file=os.path.expanduser("~/.pgpass")
    )

    object_store_conf = get_object_store_conf_path(galaxy_config_file)
    backends = parse_object_store(object_store_conf)

    # Add pulsar staging directory (runner: pulsar_embedded) to backends
    backends["pulsar_embedded"] = get_pulsar_staging_dir(galaxy_pulsar_app_conf)

    # Connect to Galaxy database
    db = Database(
        dbname=db_name,
        dbuser=db_user,
        dbhost=db_host,
        dbpassword=db_password,
    )

    # For the get_jwd subcommand
    if args.subcommand == "get_jwd":
        job_id = args.job_id
        object_store_id, job_runner_name = db.get_job_info(job_id)
        jwd_path = decode_path(job_id, [object_store_id], backends, job_runner_name)

        # Check
        if jwd_path:
            print(jwd_path)
        else:
            print(f"INFO: Job working directory (of {job_id}) does not exist")
            sys.exit(1)

    # For the clean_jwds subcommand
    if args.subcommand == "clean_jwds":
        # Check if the given Galaxy log directory exists
        if not os.path.isdir(galaxy_log_dir):
            raise ValueError(
                f"The given Galaxy log directory {galaxy_log_dir} does not exist"
            )

        # Set variables
        dry_run = args.dry_run
        days = args.days
        jwd_cleanup_log = f"{galaxy_log_dir}/jwd_cleanup_{datetime.now().strftime('%d_%m_%Y-%I_%M_%S')}.log"
        failed_jobs = db.get_failed_jobs(days=days)

        # Delete JWD folders if dry_run is False
        # Log the folders that will be deleted
        if not dry_run:
            with open(jwd_cleanup_log, "w") as jwd_log:
                jwd_log.write(
                    "The following job working directories (JWDs) belonging "
                    "to the failed jobs are deleted\nJob id: JWD path\n"
                )
                for job_id, metadata in failed_jobs.items():
                    # Delete JWD folders older than X days
                    jwd_path = decode_path(job_id, metadata, backends)
                    if jwd_path:
                        delete_jwd(jwd_path)
                        jwd_log.write(f"{job_id}: {jwd_path}\n")
        else:
            # Print JWD folders older than X days
            for job_id, metadata in failed_jobs.items():
                jwd_path = decode_path(job_id, metadata, backends)
                if jwd_path:
                    print(f"{job_id}: {jwd_path}")


def extract_password_from_pgpass(pgpass_file):
    """Extract the password from the ~/.pgpass file

    The ~/.pgpass file should have the following format:
    <pg_host>:5432:*:<pg_user>:<pg_password>

    Args:
        pgpass_file (str): Path to the ~/.pgpass file

    Returns:
        str: Password for the given pg_host
    """
    pgpass_format = "<pg_host>:5432:*:<pg_user>:<pg_password>"
    with open(pgpass_file, "r") as pgpass:
        for line in pgpass:
            if line.startswith(os.environ.get("PGHOST")):
                return line.split(":")[4].strip()
            else:
                raise ValueError(
                    f"Please add the password for '{os.environ.get('PGHOST')}' to the ~/.pgpass file in format: {pgpass_format} "
                )


def get_object_store_conf_path(galaxy_config_file):
    """Get the path to the object_store_conf.xml file

    Args:
        galaxy_config_file (str): Path to the galaxy.yml file

    Returns:
        str: Path to the object_store_conf.xml file
    """
    object_store_conf = ""
    with open(galaxy_config_file, "r") as config:
        for line in config:
            if line.strip().startswith("object_store_config_file"):
                object_store_conf = line.split(":")[1].strip()

                # Check if the object_store_conf.xml file exists
                if not os.path.isfile(object_store_conf):
                    raise ValueError(f"{object_store_conf} does not exist")

                return object_store_conf


def parse_object_store(object_store_conf):
    """Get the path of type 'job_work' from the extra_dir's for each backend

    Args:
        object_store_conf (str): Path to the object_store_conf.xml file

    Returns:
        dict: Dictionary of backend id and path of type 'job_work'
    """
    dom = parse(object_store_conf)
    backends = {}
    for backend in dom.getElementsByTagName("backend"):
        backend_id = backend.getAttribute("id")
        backends[backend_id] = {}
        # Get the extra_dir's path for each backend if type is "job_work"
        for extra_dir in backend.getElementsByTagName("extra_dir"):
            if extra_dir.getAttribute("type") == "job_work":
                backends[backend_id] = extra_dir.getAttribute("path")
    return backends


def get_pulsar_staging_dir(galaxy_pulsar_app_conf):
    """Get the path to the pulsar staging directory

    Args:
        galaxy_pulsar_app_conf (str): Path to the pulsar_app.yml file

    Returns:
        str: Path to the pulsar staging directory
    """
    pulsar_staging_dir = ""
    with open(galaxy_pulsar_app_conf, "r") as config:
        yaml_config = yaml.safe_load(config)
        pulsar_staging_dir = yaml_config["staging_directory"]

    # Check if the pulsar staging directory exists
    if not os.path.isdir(pulsar_staging_dir):
        raise ValueError(
            f"Pulsar staging directory '{pulsar_staging_dir}' does not exist"
        )

    return pulsar_staging_dir


def decode_path(job_id, metadata, backends_dict, job_runner_name=None):
    """Decode the path of JWD's and check if the path exists

    Args:
        job_id (int): Job id
        metadata (list): List of object_store_id and update_time
        backends_dict (dict): Dictionary of backend id and path of type 'job_work'
        job_runner_name (str, optional): Name of the job runner. Defaults to None.

    Returns:
        str: Path to the JWD
    """
    job_id = str(job_id)

    # Check if object_store_id exists in our object store config
    if metadata[0] not in backends_dict.keys():
        raise ValueError(
            f"Object store id '{metadata[0]}' does not exist in the object_store_conf.xml file"
        )

    # Pulsar embedded jobs uses the staging directory and this has a different path structure
    if job_runner_name == "pulsar_embedded":
        jwd_path = f"{backends_dict[job_runner_name]}/{job_id}"
    else:
        jwd_path = f"{backends_dict[metadata[0]]}/0{job_id[0:2]}/{job_id[2:5]}/{job_id}"

    # Validate that the path is a JWD
    # It is a JWD if the following conditions are true:
    # 1. Check if tool_script.sh exists
    # 2. Check if directories 'inputs', and 'outputs' exist
    # 3. Additionally, we can also try and find the file '__instrument_core_epoch_end'
    # and compare the timestamp in that with the 'update_time' (metadata[1]) of the job.
    if (
        os.path.exists(jwd_path)
        and os.path.exists(f"{jwd_path}/tool_script.sh")
        and os.path.exists(f"{jwd_path}/inputs")
        and os.path.exists(f"{jwd_path}/outputs")
    ):
        return jwd_path
    else:
        return None


def delete_jwd(jwd_path):
    """Delete JWD folder and all its contents

    Args:
        jwd_path (str): Path to the JWD folder
    """
    try:
        print(f"Deleting JWD: {jwd_path}")
        shutil.rmtree(jwd_path)
    except OSError as e:
        print(f"Error deleting JWD: {jwd_path} : {e.strerror}")


class Database:
    """Class to connect to the database and query DB

    Args:
        dbname (str): Name of the database
        dbuser (str): Name of the database user
        dbhost (str): Hostname of the database
        dbpassword (str): Password of the database user
    """

    def __init__(self, dbname, dbuser, dbhost, dbpassword):
        try:
            self.conn = psycopg2.connect(
                dbname=dbname, user=dbuser, host=dbhost, password=dbpassword
            )
        except psycopg2.OperationalError as e:
            print(f"Unable to connect to database: {e}")

    def get_failed_jobs(self, days):
        """Get failed jobs for DB

        Args:
            days (int): Number of days to look back for failed jobs

        Returns:
            dict: Dictionary with job_id as key and object_store_id, and update_time as list of values


        """
        cur = self.conn.cursor()
        cur.execute(
            f"""
            SELECT id, object_store_id, update_time
            FROM job
            WHERE state = 'error'
            AND update_time IS NOT NULL
            AND object_store_id IS NOT NULL
            AND update_time > NOW() - INTERVAL '{days} days'
            """
        )
        failed_jobs = cur.fetchall()
        cur.close()
        self.conn.close()

        # Create a dictionary with job_id as key and object_store_id, and update_time as values
        failed_jobs_dict = {}
        for job_id, object_store_id, update_time in failed_jobs:
            failed_jobs_dict[job_id] = [object_store_id, update_time]

        if not failed_jobs_dict:
            print(f"No failed jobs found within the last {days} days")
            sys.exit(1)

        return failed_jobs_dict

    def get_job_info(self, job_id):
        """Get object_store_id and job_runner_name for a given job id

        Args:
            job_id (int): Job id

        Returns:
            object_store_id (str): Object store id
            job_runner_name (str): Job runner name
        """
        cur = self.conn.cursor()
        cur.execute(
            f"""
            SELECT object_store_id, job_runner_name
            FROM job
            WHERE id = '{job_id}' AND object_store_id IS NOT NULL AND job_runner_name IS NOT NULL
            """
        )
        object_store_id, job_runner_name = cur.fetchone()
        cur.close()
        self.conn.close()

        if not object_store_id:
            print(
                f"Object store id and/or the job runner name for the job '{job_id}' was not found in the database"
            )
            sys.exit(1)

        return object_store_id, job_runner_name


if __name__ == "__main__":
    main()
