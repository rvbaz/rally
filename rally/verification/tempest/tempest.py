# Copyright 2014: Mirantis Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


import os
import shutil
import subprocess
import sys
import tempfile

from oslo_utils import encodeutils

from rally.common import costilius
from rally.common.i18n import _
from rally.common.io import subunit_v2
from rally.common import log as logging
from rally import consts
from rally import exceptions
from rally.verification.tempest import config


TEMPEST_SOURCE = "https://git.openstack.org/openstack/tempest"

LOG = logging.getLogger(__name__)


class TempestSetupFailure(exceptions.RallyException):
    msg_fmt = _("Unable to setup Tempest: %(message)s")


def check_output(*args, **kwargs):
    kwargs["stderr"] = subprocess.STDOUT
    try:
        output = costilius.sp_check_output(*args, **kwargs)
    except subprocess.CalledProcessError as e:
        LOG.error("Failed cmd: '%s'" % e.cmd)
        LOG.error("Error output: '%s'" % encodeutils.safe_decode(e.output))
        raise

    LOG.debug("subprocess output: '%s'" % encodeutils.safe_decode(output))
    return output


class Tempest(object):

    base_repo_dir = os.path.join(os.path.expanduser("~"),
                                 ".rally/tempest/base")

    def __init__(self, deployment, verification=None, tempest_config=None,
                 source=None, system_wide_install=False):
        self.tempest_source = source or TEMPEST_SOURCE
        self.deployment = deployment
        self._path = os.path.join(os.path.expanduser("~"),
                                  ".rally/tempest",
                                  "for-deployment-%s" % deployment)
        self.config_file = tempest_config or self.path("tempest.conf")
        self.log_file_raw = self.path("subunit.stream")
        self.verification = verification
        self._env = None
        self._base_repo = None
        self._system_wide_install = system_wide_install

    def _generate_env(self):
        env = os.environ.copy()
        env["TEMPEST_CONFIG_DIR"] = os.path.dirname(self.config_file)
        env["TEMPEST_CONFIG"] = os.path.basename(self.config_file)
        env["OS_TEST_PATH"] = self.path("tempest/test_discover")
        LOG.debug("Generated environ: %s" % env)
        self._env = env

    @property
    def venv_wrapper(self):
        if self._system_wide_install:
            return ""
        else:
            return self.path("tools/with_venv.sh")

    @property
    def env(self):
        if not self._env:
            self._generate_env()
        return self._env

    def path(self, *inner_path):
        if inner_path:
            return os.path.join(self._path, *inner_path)
        return self._path

    @staticmethod
    def _is_git_repo(directory):
        # will suppress git output
        with open(os.devnull, "w") as devnull:
            return os.path.isdir(directory) and not subprocess.call(
                ["git", "status"], stdout=devnull, stderr=subprocess.STDOUT,
                cwd=os.path.abspath(directory))

    @staticmethod
    def _move_contents_to_dir(base, directory):
        """Moves contents of directory :base into directory :directory

        :param base: source directory to move files from
        :param directory: directory to move files to
        """
        for filename in os.listdir(base):
            source = os.path.join(base, filename)
            LOG.debug("Moving file {source} to {dest}".format(source=source,
                                                              dest=directory))
            shutil.move(source, os.path.join(directory, filename))

    @property
    def base_repo(self):
        """Get directory to clone tempest to

        old:
            _ rally/tempest
            |_base -> clone from source to here
            |_for-deployment-<UUID1> -> copy from relevant tempest base
            |_for-deployment-<UUID2> -> copy from relevant tempest base

        new:
            _ rally/tempest
            |_base
            ||_ tempest_base-<rand suffix specific for source> -> clone
            ||        from source to here
            ||_ tempest_base-<rand suffix 2>
            |_for-deployment-<UUID1> -> copy from relevant tempest base
            |_for-deployment-<UUID2> -> copy from relevant tempest base

        """
        if os.path.exists(Tempest.base_repo_dir):
            if self._is_git_repo(Tempest.base_repo_dir):
                # this is the old dir structure and needs to be upgraded
                directory = tempfile.mkdtemp(prefix=os.path.join(
                    Tempest.base_repo_dir, "tempest_base-"))
                LOG.debug("Upgrading Tempest directory tree: "
                          "Moving Tempest base dir %s into subdirectory %s" %
                          (Tempest.base_repo_dir, directory))
                self._move_contents_to_dir(Tempest.base_repo_dir,
                                           directory)
            if not self._base_repo:
                # Search existing tempest bases for a matching source
                repos = [d for d in os.listdir(Tempest.base_repo_dir)
                         if self._is_git_repo(d) and
                         self.tempest_source == self._get_remote_origin(d)]
                if len(repos) > 1:
                    raise exceptions.MultipleMatchesFound(
                        needle="git directory",
                        haystack=repos)
                if repos:
                    # Use existing base with relevant source
                    self._base_repo = repos.pop()
        else:
            os.makedirs(Tempest.base_repo_dir)
        if not self._base_repo:
            self._base_repo = tempfile.mkdtemp(prefix=os.path.join(
                os.path.abspath(Tempest.base_repo_dir), "tempest_base-"))
        return self._base_repo

    @staticmethod
    def _get_remote_origin(directory):
        out = check_output(["git", "config", "--get", "remote.origin.url"],
                           cwd=os.path.abspath(directory))
        return out.strip()

    def _install_venv(self):
        path_to_venv = self.path(".venv")

        if not os.path.isdir(path_to_venv):
            LOG.debug("No virtual environment for Tempest found.")
            LOG.info(_("Installing the virtual environment for Tempest."))
            LOG.debug("Virtual environment directory: %s" % path_to_venv)
            required_vers = (2, 7)
            if sys.version_info[:2] != required_vers:
                # NOTE(andreykurilin): let's try to find a suitable python
                # interpreter for Tempest
                python_interpreter = costilius.get_interpreter(required_vers)
                if not python_interpreter:
                    raise exceptions.IncompatiblePythonVersion(
                        version=sys.version, required_version=required_vers)
                LOG.info(
                    _("Tempest requires Python %(required)s, '%(found)s' was "
                      "found in your system and it will be used for installing"
                      " virtual environment.") % {"required": required_vers,
                                                  "found": python_interpreter})
            else:
                python_interpreter = sys.executable
            try:
                check_output([python_interpreter, "./tools/install_venv.py"],
                             cwd=self.path())
                # NOTE(kun): Using develop mode installation is for run
                #            multiple tempest instance. However, dependency
                #            from tempest(os-testr) has issues here, before
                #            https://review.openstack.org/#/c/207691/ being
                #            merged, we have to install dependency manually and
                #            run setup.py with -N(install package without
                #            dependency)
                check_output([self.venv_wrapper, "pip", "install", "-r",
                              "requirements.txt", "-r",
                              "test-requirements.txt"], cwd=self.path())
                check_output([self.venv_wrapper, "pip", "install",
                              "-e", "./"], cwd=self.path())
            except subprocess.CalledProcessError:
                if os.path.exists(self.path(".venv")):
                    shutil.rmtree(self.path(".venv"))
                raise TempestSetupFailure(_("failed to install virtualenv"))

    def is_configured(self):
        return os.path.isfile(self.config_file)

    def generate_config_file(self, override=False):
        """Generate configuration file of Tempest for current deployment.

        :param override: Whether or not to override existing Tempest
                         config file
        """
        if not self.is_configured() or override:
            if not override:
                LOG.info(_("Tempest is not configured."))

            LOG.info(_("Starting: Creating configuration file for Tempest."))
            config.TempestConfig(self.deployment).generate(self.config_file)
            LOG.info(_("Completed: Creating configuration file for Tempest."))
        else:
            LOG.info("Tempest is already configured.")

    def _initialize_testr(self):
        if not os.path.isdir(self.path(".testrepository")):
            LOG.debug("Initialization of 'testr'.")
            cmd = ["testr", "init"]
            if self.venv_wrapper:
                cmd.insert(0, self.venv_wrapper)
            try:
                check_output(cmd, cwd=self.path())
            except (subprocess.CalledProcessError, OSError):
                if os.path.exists(self.path(".testrepository")):
                    shutil.rmtree(self.path(".testrepository"))
                raise TempestSetupFailure(_("Failed to initialize 'testr'"))

    def is_installed(self):
        if self._system_wide_install:
            return os.path.exists(self.path(".testrepository"))

        return os.path.exists(self.path(".venv")) and os.path.exists(
            self.path(".testrepository"))

    def _clone(self):
        LOG.info(_("Please, wait while Tempest is being cloned."))
        try:
            subprocess.check_call(["git", "clone",
                                   self.tempest_source,
                                   self.base_repo])
        except subprocess.CalledProcessError:
            if os.path.exists(self.base_repo):
                shutil.rmtree(self.base_repo)
            raise

    def install(self):
        """Creates local Tempest repo and virtualenv for deployment."""
        if not self.is_installed():
            try:
                if not os.path.exists(self.path()):
                    if not self._is_git_repo(self.base_repo):
                        self._clone()
                    shutil.copytree(self.base_repo, self.path())
                    for cmd in ["git", "checkout", "master"], ["git", "pull"]:
                        subprocess.check_call(cmd, cwd=self.path("tempest"))
                if not self._system_wide_install:
                    self._install_venv()
                self._initialize_testr()
            except subprocess.CalledProcessError as e:
                self.uninstall()
                raise TempestSetupFailure("failed cmd: '%s'" % e.cmd)
            else:
                LOG.info(_("Tempest has been successfully installed!"))

        else:
            LOG.info(_("Tempest is already installed."))

    def uninstall(self):
        """Removes local Tempest repo and virtualenv for deployment

         Checks that local repo exists first.
        """
        if os.path.exists(self.path()):
            shutil.rmtree(self.path())

    @logging.log_verification_wrapper(LOG.info, _("Run verification."))
    def _prepare_and_run(self, set_name, regex, tests_file):
        if not self.is_configured():
            self.generate_config_file()

        if set_name == "full":
            testr_arg = ""
        else:
            if set_name in consts.TempestTestsAPI:
                testr_arg = "tempest.api.%s" % set_name
            elif tests_file:
                testr_arg = "--load-list %s" % os.path.abspath(tests_file)
            else:
                testr_arg = set_name or regex

        self.verification.start_verifying(set_name)
        try:
            self.run(testr_arg)
        except subprocess.CalledProcessError:
            LOG.info(_("Test set '%s' has been finished with errors. "
                       "Check logs for details.") % set_name)

    def run(self, testr_arg=None, log_file=None, tempest_conf=None):
        """Launch tempest with given arguments

        :param testr_arg: argument which will be transmitted into testr
        :type testr_arg: str
        :param log_file: file name for raw subunit results of tests. If not
                         specified, value from "self.log_file_raw"
                         will be chosen.
        :type log_file: str
        :param tempest_conf: User specified tempest.conf location
        :type tempest_conf: str

        :raises subprocess.CalledProcessError: if tests has been finished
                                               with error.
        """

        if tempest_conf and os.path.isfile(tempest_conf):
            self.config_file = tempest_conf
        LOG.info(_("Tempest config file: %s") % self.config_file)

        test_cmd = (
            "%(venv)s testr run --parallel --subunit %(arg)s "
            "| tee %(log_file)s "
            "| %(venv)s subunit-trace -f -n" %
            {
                "venv": self.venv_wrapper,
                "arg": testr_arg,
                "log_file": log_file or self.log_file_raw
            })
        LOG.debug("Test(s) started by the command: %s" % test_cmd)
        # Create all resources needed for Tempest before running tests.
        # Once tests finish, all created resources will be deleted.
        with config.TempestResourcesContext(self.deployment, self.config_file):
            # Run tests
            subprocess.check_call(test_cmd, cwd=self.path(),
                                  env=self.env, shell=True)

    def discover_tests(self, pattern=""):
        """Return a set of discovered tests which match given pattern."""

        cmd = [self.venv_wrapper, "testr", "list-tests", pattern]
        raw_results = subprocess.Popen(
            cmd, cwd=self.path(), env=self.env,
            stdout=subprocess.PIPE).communicate()[0]

        tests = set()
        for test in raw_results.split("\n"):
            if test.startswith("tempest."):
                index = test.find("[")
                if index != -1:
                    tests.add(test[:index])
                else:
                    tests.add(test)

        return tests

    def parse_results(self, log_file=None):
        """Parse subunit raw log file."""
        log_file_raw = log_file or self.log_file_raw
        if os.path.isfile(log_file_raw):
            return subunit_v2.parse_results_file(log_file_raw)
        else:
            LOG.error("JSON-log file not found.")
            return None

    @logging.log_verification_wrapper(
        LOG.info, _("Saving verification results."))
    def _save_results(self, log_file=None):
        results = self.parse_results(log_file)
        if results and self.verification:
            self.verification.finish_verification(total=results.total,
                                                  test_cases=results.tests)
        else:
            self.verification.set_failed()

    def verify(self, set_name, regex, tests_file):
        self._prepare_and_run(set_name, regex, tests_file)
        self._save_results()

    def import_results(self, set_name, log_file):
        if log_file:
            self.verification.start_verifying(set_name)
            self._save_results(log_file)
        else:
            LOG.error("No log file to import results was specified.")
