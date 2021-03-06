import os
import sys
import subprocess
import configparser

from shutil    import copyfile
from itertools import count

from .         import constants as const
from .stats    import Statistics
from .dbsystem import DbSystem

class Runner:
    """Runner: Makes Popen calls to run YCSB, collects output, extracts data
    from YCSB output, handles logging"""
    def __init__(self, configpath, hooks=None):
        """__init__

        :param configpath: Path to YCSB Runner configuration file
        :param hooks: A dictionary mapping hook names to lists of functions.
        """
        # Check that the configpath exists before reading it
        if not os.path.exists(configpath):
            raise IOError("Runner config file '%s' does not exist" % configpath)
        # Read the runner config with Python's ConfigParser first
        self.__config = configparser.ConfigParser(defaults=const.OPTION_DEFAULTS)
        self.__config.read(configpath)
        # Now, process the config further, extracting DBMS names, options
        self.dbs = self.__process_sections()
        # Load hooks
        self.__hooks = {} if hooks is None else hooks
        # We need this in order to copy the file across to the output dir
        self.__configpath = configpath

    def run(self):
        self.__run_hooks("PRE_RUN")
        for db in self.dbs:
            self.__run_hooks("PRE_DB", db)
            # Copy config files to output dir
            copyfile(self.__configpath, db.makefpath("config-{}-{}.ini"))
            # Copy the raw workload and generated workload to output for
            # reference. This is useful because the generated workload file
            # doesn't include original comments, for exmaple.
            copyfile(db.base_workload_path, db.makefpath("workload-{}-{}"))
            db.generate_workload_file(db.makefpath("workload-generated-{}-{}"))
            for trial in range(1, db.trials + 1):
                self.__run_hooks("PRE_TRIAL", trial, db)
                db.log("Starting trial %i..." % (trial), trial=trial)
                for mpl in count(start=db.min_mpl, step=db.inc_mpl):
                    self.__run_hooks("PRE_MPL", mpl, trial, db)
                    # Obvious; don't go above configured maximum MPL
                    if mpl > db.max_mpl:
                        break
                    # Clean the database
                    db.log("Cleaning the database...", mpl=mpl, trial=trial)
                    if db.clean_data:
                        db.clean()
                    # Load data and run YCSB
                    db.log("Loading YCSB data...", mpl=mpl, trial=trial)
                    if db.clean_data:
                        db.raw_log(self.__popen(db.cmd_ycsb_load()))
                    db.log("Running YCSB workload...", mpl=mpl, trial=trial)
                    # Run YCSB+T, log output, collect stats
                    stats = Runner.extract_stats(db.raw_log(self.__popen(db.cmd_ycsb_run(mpl))))
                    # Set the MPL and trial number in the stats row
                    stats.mpl = mpl
                    stats.trial = trial
                    db.stats.addstats(stats)
                    self.__run_hooks("POST_MPL", mpl, trial, db)
                    # Export run stats repeatedly for maximum durability
                    db.log("Exporting run stats...")
                    db.export_stats()
                self.__run_hooks("POST_TRIAL", trial, db)
            db.cleanup() # ensure file handles are closed properly and don't leak
            self.__run_hooks("POST_DB", db)
        self.__run_hooks("POST_RUN")

    def __popen(self, cmd):
        """__popen
        Open a process given by the list of shell arguments, cmd

        Returns the stdout resulting from running the process

        :param cmd: List of shell arguments, including name of command as
        first element
        """
        with subprocess.Popen(cmd, stdout=subprocess.PIPE) as proc:
            # Collect and return stdout after running process
            stdout = proc.stdout.read().decode("utf-8")
            # If this doesn't hold then something is horribly wrong and we
            #   should abort mission
            assert type(stdout) is str
            return stdout

    def __process_sections(self):
        """__process_sections
        Processes each section in the config file,
        populating this object with corresponding DbSystem instances
        """
        dbs = []
        for section in self.__config.sections():
            config = self.__process_runner_config_keys(section)
            dbs += self.__process_dbs(section, config)
        return dbs

    def __process_runner_config_keys(self, section):
        """__process_runner_config_keys
        :param section: Name of section from runner config file for which
        k=v options should be processed
        """
        config = {}
        for k, t in const.OPTION_KEYS.items():
            # Handle integer-valued keys
            if t is int:
                config[k] = self.__config.getint(section, k)
            # Handle boolean-valued keys
            elif t is bool:
                print("getting bool", k)
                config[k] = self.__config.getboolean(section, k)
            # Handle string-valued keys
            elif t is str:
                config[k] = self.__config.get(section, k)
            elif callable(t):
                config[k] = t(self.__config.get(section, k))
            else:
                print("Warning: skipping key %s with invalid type" % k)
        return config

    def __extraneous_config(self, config_section):
        """__extraneous_config
        Returns a dictionary containing key -> value mappings from the
        specified config section which aren't contained in the
        const.OPTION_KEYS dict

        :param config_section: An object corresponding to one section from a
        configparser object
        """
        config_keys = set(config_section.keys())
        option_keys = set(const.OPTION_KEYS.keys())
        extraneous_keys = list(config_keys - option_keys)
        return {k: config_section.get(k) for k in extraneous_keys}

    def __process_dbs(self, section, config):
        """__process_dbs
        Creates DbSystem instances with their corresponding configurations for
        each DBMS in the runner config

        :param section: Section string from Python configparser sections
        :param config: Dict of key -> value mappings from parsed config
            sections (e.g. output of __process_runner_config_keys)
        """
        # Find extraneous config pairs
        extraneous_config = self.__extraneous_config(self.__config[section])
        # Section headings may contain multiple DB names, CSV format
        section = [s.strip() for s in section.split(',')]
        db_instances = []
        for dbname in section:
            # Extract and remove the DBMS label
            label = const.RE_DBNAME_LABEL.search(dbname)
            if label is not None:
                label, = label.groups(0)
                dbname = const.RE_DBNAME_LABEL.sub("", dbname)
            else:
                label = ""
            # Validate DBMS name
            if dbname.lower() not in const.SUPPORTED_DBS:
                print("Invalid database found: %s. Only (%s) are supported. Skipping..." %
                        (dbname, ','.join(const.SUPPORTED_DBS)))
                continue

            # Build the DbSystem object
            db_instances.append(DbSystem(dbname, config, label=label,
                extraneous_config=extraneous_config))
        return db_instances

    @classmethod
    def extract_stats(cls, stdout):
        """extract_stats
        Extracts statistics from the given YCSB+T output

        :param stdout: YCSB+T output from which statistics extraction should
        take place
        """
        stats = {}
        for k, regex in const.STAT_REGEXPS.items():
            m = Runner.get_re_match(regex, stdout)
            if m is not None:
                stats[k] = const.TRACKED_STATS[k](m)
        # Return new Statistics row storing extracted stats
        return Statistics(**stats)

    @classmethod
    def get_re_match(cls, regex, string):
        """get_re_match
        Returns the contents of the first capturing group after running
        regex.search on the given string, or None if no matches found

        :param regex: Precompiled regex to run on string
        :param string: String on which to run regex
        """
        res = regex.search(string)
        if res != None and len(res.groups()) > 0:
            return res.group(1)
        return None

    def __run_hooks(self, location, *args):
        """__run_hooks
        Runs all hooks for the given hook location, passing in the given args.

        :param location: Location name for the hooks to run.
        :param *args: Arguments to pass to each hook function.
        """
        location = location.upper()
        if location in self.__hooks:
            for h in self.__hooks[location]:
                h(*args)
