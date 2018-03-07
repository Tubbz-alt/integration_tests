#!/usr/bin/env python2
# -*- coding: utf-8 -*-
import argparse
import jenkins
import os
import re
import requests
import subprocess
import time

from requests.auth import HTTPBasicAuth
from six.moves.urllib.parse import urlsplit, urlunsplit

from cfme.utils.appliance import IPAppliance
from cfme.utils.conf import env
from cfme.utils.log import logger, add_stdout_handler
from cfme.utils.path import log_path
from cfme.utils.quote import quote
from cfme.utils.version import Version

# log to stdout too
add_stdout_handler(logger)

# Global variables
coverage_dir = '/coverage'
scan_timeout = env.sonarqube.get('scan_timeout', 600)
scanner_dir = '/root/scanner'
sonar_server_url = env.sonarqube.url
sonar_scanner_url = env.sonarqube.scanner_url


def group_list_dict_by(ld, by):
    """Indexes a list of dictionaries.

    Takes a list of dictionaries and creates a structure
    that indexes them by a particular keyword.

    Args:
        ld: list of dictionaries.
        by: key by which to index the dictionaries.

    Returns:
        A dictionary whose keys are the values of the key
        by, and whose values are the dictionaries in the
        original list of dictionaries (i.e. that is an index
        of the dictionaries).
    """
    result = {}
    for d in ld:
        result[d[by]] = d
    return result


def jenkins_artifact_url(jenkins_username, jenkins_token, jenkins_url, jenkins_job, jenkins_build,
        artifact_path):
    """Build Jenkins artifact URL for a particular Jenkins job.

    Args:
        jenkins_username:  Jenkins login.
        jenkins_token:  User token generated in the Jenkins UI.
        jenkins_url:  URL to Jenkins server.
        jenkins_job:  Jenkins Job ID
        jenkins_build: Particular Jenkins Run/Build
        artifactor_path: Path within the artifactor archive to the artifact.

    Returns:
        URL to artifact within the artifactor archive of Jenkins job.
    """
    url = '{}/job/{}/{}/artifact/{}'.format(jenkins_url, jenkins_job, jenkins_build, artifact_path)
    scheme, netloc, path, query, fragment = urlsplit(url)
    netloc = '{}:{}@{}'.format(jenkins_username, jenkins_token, netloc)
    return urlunsplit([scheme, netloc, path, query, fragment])


def download_artifact(
        jenkins_username, jenkins_token, jenkins_url, jenkins_job, jenkins_build,
        artifact_path):
    """Download artifactor artifact

    Gets a particular artifact from a Jenkins job.

    Args:
        jenkins_username:  Jenkins login.
        jenkins_token:  User token generated in the Jenkins UI.
        jenkins_url:  URL to Jenkins server.
        jenkins_job:  Jenkins Job ID
        jenkins_build: Particular Jenkins Run/Build
        artifactor_path: Path within the artifactor archive to the artifact.

    Returns:
        text of download.
    """
    url = '{}/job/{}/{}/artifact/{}'.format(jenkins_url, jenkins_job, jenkins_build, artifact_path)
    return requests.get(
        url, verify=False, auth=HTTPBasicAuth(jenkins_username, jenkins_token)).text


def check_artifact(
        jenkins_username, jenkins_token, jenkins_url, jenkins_job, jenkins_build,
        artifact_path):
    """Verify that artifact exists

    Verify artifact exists for a particular job.

    Args:
        jenkins_username:  Jenkins login.
        jenkins_token:  User token generated in the Jenkins UI.
        jenkins_url:  URL to Jenkins server.
        jenkins_job:  Jenkins Job ID
        jenkins_build: Particular Jenkins/Build
        artifactor_path: Path within the artifactor archive to the artifact.

    Returns:
        True if it exists, False if it does not.
    """
    url = jenkins_artifact_url(
        jenkins_username, jenkins_token, jenkins_url, jenkins_job, jenkins_build, artifact_path)
    return requests.head(
        url, verify=False, auth=HTTPBasicAuth(jenkins_username, jenkins_token)).status_code < 300


def get_build_numbers(client, job_name):
    return [build['number'] for build in client.get_job_info(job_name)['builds']]


def gen_project_key(name, version):
    """Generate project name based on Central CI rules

    The Central CI docs found here:

        https://docs.engineering.redhat.com/display/CentralCI/Code+Quality+Management

    Document that a project key should take the form of:

        <project-name>_<major_version>_<minor_version>_<language>_<coverage|static|full-analysis>

    So given the name CFME and version 5.9.0.21, and that CFME is in ruby and we are
    gathering coverage data, our project_key would be:

        CFME_5_9_ruby_coverage

    Args:
        name:   application name
        version:  A version like a.b.c.d where a major version and b is the minor version.
            Actually minimally just need a.b, but any components after a.b are fine.
    Returns:
        a valid Central CI project key for sonarqube.
    """
    # I'm on purpose allowing for any number of version components after 2
    # in case the version string changes (but still has major and minor at
    # at the beginning.
    match = re.search('^(?P<major>\d+)\.(?P<minor>\d+)', version)
    if not match:
        raise Exception(
            'Invalid version string given.  Expect #.#[... .#] received: {}'.format(version))

    project_key = '{name}_{major}_{minor}_ruby_coverage'.format(
        name=name,
        major=match.group('major'),
        minor=match.group('minor'))

    return project_key


def merge_coverage_data(ssh, coverage_dir):
    """Merge coverage data

    Take all the by appliance by process .resultset.json files from
    the coverage archive and merge them into one .resultset.json file.
    Expects the coverage archive to have been extracted to the
    coverage_dir on the appliance to which the ssh client is connected.

    Args:
        ssh:  ssh client
        coverage_dir:  Directory where the coverage archive was extracted.

    Returns:
        Nothing
    """
    logger.info('Merging coverage data')

    # Run the coverage merger script out of the rails root pointing
    # to where the coverage data is installed.   This will generate
    # a directory under the coverage directory called merged, and
    # will have the merged .resultset.json file in it, along with some
    # HTML that was generated by the merger script.
    cmd = ssh.run_rails_command(
        'coverage_merger.rb --coverageRoot={}'.format(coverage_dir),
        timeout=60 * 60)
    if cmd.failed:
        raise Exception('Failure running the merger - {}'.format(str(cmd)))

    # Attempt to get the overall code coverage percentage from the result.
    logger.info('Coverage report generation was successful')
    logger.info(str(cmd))
    percentage = re.search(r'LOC\s+\((\d+.\d+%)\)\s+covered\.', str(cmd))
    if percentage:
        logger.info('COVERAGE=%s', percentage.groups()[0])
    else:
        logger.info('COVERAGE=unknown')

    # The sonar-scanner will actually need the .resultset.json file it
    # uses to be in /coverage/.resultset.json (i.e. the root of the coverarage
    # directory), so lets create a symlink:
    ssh.run_command('ln -s merged/.resultset.json {}/.resultset.json'.format(coverage_dir))


def pull_merged_coverage_data(ssh, coverage_dir):
    """Pulls merged coverage data to log directory.

    Args:
        ssh:  ssh client
        coverage_dir:  Directory where the coverage archive was extracted.

    Returns:
        Nothing
    """
    logger.info('Packing the generated HTML')
    cmd = ssh.run_command('cd {}; tar cfz /tmp/merged.tgz merged'.format(coverage_dir))
    if cmd.failed:
        raise Exception('Could not compress! - {}'.format(str(cmd)))
    logger.info('Grabbing the generated HTML')
    ssh.get_file('/tmp/merged.tgz', log_path.strpath)
    logger.info('Locally decompressing the generated HTML')
    subprocess.check_call(
        ['tar', 'xf', log_path.join('merged.tgz').strpath, '-C', log_path.strpath])
    logger.info('Done!')


def install_sonar_scanner(ssh, project_name, project_version, scanner_url, scanner_dir, server_url):
    """ Install sonar-scanner application

    Pulls the sonar-scanner application to the appliance from scanner_url,
    installs it in scanner_dir, and configures it to send its scan data to
    server_url.  It also configures the project config for the scan, setting
    sonar.projectVersion to the appliance version, and setting sonar.sources
    to pick up both sets of sources.

    Args:
        ssh: ssh object (cfme.utils.ssh)
        project_version: Version of project to be scanned.
        scanner_url:  Where to get the scanner from.
        scanner_dir:  Where to install the scanner on the appliance.
        server_url:  Where to send scan data to (i.e. what sonarqube)

    Returns:
        Nothing
    """
    logger.info('Installing sonar scanner on appliance.')
    scanner_zip = '/root/scanner.zip'

    # Create install directory for sonar scanner:
    if not ssh.run_command('mkdir -p {}'.format(scanner_dir)):
        raise Exception(
            'Could not create sonar scanner directory, {}, on appliance.'.format(scanner_dir))

    # Download the scanner
    if not ssh.run_command('wget -O {} {}'.format(scanner_zip, quote(scanner_url))):
        raise Exception('Could not download scanner software, {}'.format(scanner_url))

    # Extract the scanner
    if not ssh.run_command('unzip -d {} {}'.format(scanner_dir, scanner_zip)):
        raise Exception(
            'Could not extract scanner software, {}, to {}'.format(scanner_zip, scanner_dir))

    # Note, all the files are underneath one directory under our scanner_dir, but we don't
    # necessarily know the name of that directory.   Yes today, as I write this, the name
    # will be:
    #
    #   sonar-scanner-$version-linux
    #
    # but if they decide to change its name, any code that depended on that would break.   So
    # what will do is go into the one directory that now under our scanner_dir, and move all
    # those files up a directory (into our scanner_dir).   tar has the --strip-components
    # option that would have avoided this, however we are dealing with a zip file and unzip
    # has no similar option.
    if not ssh.run_command('cd {}; mv $(ls)/* .'.format(scanner_dir)):
        raise Exception('Could not move scanner files into scanner dir, {}'.format(scanner_dir))

    # Configure the scanner to point to the local sonarqube
    # WARNING:  This definitely makes the assumption the only thing we need in that config is
    #           the variable sonar.host.url set.  If that is ever wrong this will fail, perhaps
    #           mysteriously.  So the ease of this implementation is traded off against that
    #           possible future consequence.
    scanner_conf = '{}/conf/sonar-scanner.properties'.format(scanner_dir)
    if not ssh.run_command('echo "sonar.host.url={}" > {}'.format(server_url, scanner_conf)):
        raise Exception('Could write scanner conf, {}s'.format(scanner_conf))

    # Now configure the project
    #
    # We have sources in two directories:
    #
    #   - /opt/rh/cfme-gemset
    #   - /var/www/miq/vmdb
    #
    # It is very important that we set sonar.sources to a comma delimited
    # list of these directories but as relative paths, relative to /.   If
    # we configure them as absolute paths it will only see the files /var/www/miq/vmdb.
    # Don't know why, it just is that way.
    #
    # Hear is an example config:
    #
    #   sonar.projectKey=CFME5.9-11
    #   sonar.projectName=CFME-11
    #   sonar.projectVersion=5.9.0.17
    #   sonar.language=ruby
    #   sonar.sources=opt/rh/cfme-gemset,var/www/miq/vmdb
    project_conf = 'sonar-project.properties'
    local_conf = os.path.join(log_path.strpath, project_conf)
    remote_conf = '/{}'.format(project_conf)
    config_data = '''
sonar.projectKey={project_key}
sonar.projectName={project_name}
sonar.projectVersion={version}
sonar.language=ruby
sonar.sources=opt/rh/cfme-gemset,var/www/miq/vmdb
'''.format(
        project_name=project_name,
        project_key=gen_project_key(name=project_name, version=project_version),
        version=project_version)

    # Write the config file locally and then copy to remote.
    logger.info('Writing %s', local_conf)
    with open(local_conf, 'w') as f:
        f.write(config_data)
    logger.info('Copying %s to appliance as %s', local_conf, remote_conf)
    ssh.put_file(local_conf, remote_conf)


def run_sonar_scanner(ssh, scanner_dir, timeout):
    """Run the sonar scanner

    Run the sonar-scanner.

    Args:
        ssh: ssh object (cfme.utils.ssh)
        scanner_dir:  Installation directory of the sonar-scanner software.
        timeout:  timeout in seconds.

    Returns:
        Nothing
    """
    logger.info('Running sonar scan. This may take a while.')
    logger.info('   timeout=%s', timeout)
    logger.info('   start_time=%s', time.strftime('%T'))
    scanner_executable = '{}/bin/sonar-scanner'.format(scanner_dir)

    # It's very important that we run the sonar-scanner from / as this
    # will allow sonar-scanner to have all CFME ruby source code under
    # one directory as sonar-scanner expects a project to contain all its
    # source under one directory.
    cmd = 'cd /; SONAR_SCANNER_OPTS="-Xmx4096m" {} -X'.format(scanner_executable)
    result = ssh.run_command(cmd, timeout=timeout)
    if not result:
        raise Exception("sonar scan failed!\ncmd: {}\noutput: {}".format(cmd, result))
    logger.info('   end_time=%s', time.strftime('%T'))


def sonar_scan(ssh, project_name, project_version, scanner_url, scanner_dir, server_url, timeout):
    """Run the sonar scan

    In addition to running the scan, handles the installation of the sonar-scanner software.

    Args:
        ssh: ssh object (cfme.utils.ssh)
        project_name: Name of software.
        project_version: Version of project to be scanned.
        scanner_url:  Where to pull the sonar-scanner software from
        scanner_dir:  Installation directory of sonar-scanner
        server_url:  sonarqube URL.
        timeout:  timeout in seconds

    Returns:
        Nothing
    """
    install_sonar_scanner(ssh, project_name, project_version, scanner_url, scanner_dir, server_url)
    run_sonar_scanner(ssh, scanner_dir, timeout)


def main(appliance, jenkins_url, jenkins_user, jenkins_token, job_name):
    if not jenkins_user or not jenkins_token:
        try:
            from cfme.utils import conf
            jenkins_user = conf.credentials.jenkins_app.user
            jenkins_token = conf.credentials.jenkins_app.token
        except (AttributeError, KeyError):
            raise ValueError(
                '--jenkins-user and --jenkins-token not provided and credentials yaml does not '
                'contain the jenkins_app entry with user and token')
    appliance_version = str(appliance.version).strip()
    logger.info('Looking for appliance version %s in %s', appliance_version, job_name)
    client = jenkins.Jenkins(jenkins_url, username=jenkins_user, password=jenkins_token)
    build_numbers = get_build_numbers(client, job_name)
    if not build_numbers:
        raise Exception('No builds for job {}'.format(job_name))

    # Find the builds with appliance version
    eligible_build_numbers = set()
    for build_number in build_numbers:
        try:
            artifacts = client.get_build_info(job_name, build_number)['artifacts']
            if not artifacts:
                raise ValueError()
        except (KeyError, ValueError):
            logger.info('No artifacts for %s/%s', job_name, build_number)
            continue

        artifacts = group_list_dict_by(artifacts, 'fileName')
        if 'appliance_version' not in artifacts:
            logger.info('appliance_version not in artifacts of %s/%s', job_name, build_number)
            continue

        build_appliance_version = download_artifact(
            jenkins_user, jenkins_token, jenkins_url, job_name, build_number,
            artifacts['appliance_version']['relativePath']).strip()

        if not build_appliance_version:
            logger.info('Appliance version unspecified for build %s', build_number)
            continue

        if Version(build_appliance_version) < Version(appliance_version):
            logger.info(
                'Build %s already has lower version (%s)', build_number, build_appliance_version)
            logger.info('Ending here')
            break

        if 'coverage-results.tgz' not in artifacts:
            logger.info('coverage-results.tgz not in artifacts of %s/%s', job_name, build_number)
            continue

        if not check_artifact(
                jenkins_user, jenkins_token, jenkins_url, job_name, build_number,
                artifacts['coverage-results.tgz']['relativePath']):
            logger.info('Coverage archive not possible to be downloaded, skipping')
            continue

        if build_appliance_version == appliance_version:
            logger.info('Build %s was found to contain what is needed', build_number)
            eligible_build_numbers.add(build_number)
        else:
            logger.info(
                'Skipping build %s because it does not have correct version (%s)',
                build_number,
                build_appliance_version)

    if not eligible_build_numbers:
        raise Exception(
            'Could not find any coverage reports for {} in {}'.format(appliance_version, job_name))

    eligible_build_numbers = sorted(eligible_build_numbers)

    # Stop the evm service, not needed at all
    logger.info('Stopping evmserverd')
    appliance.evmserverd.stop()
    # Install the coverage tools on the appliance
    logger.info('Installing simplecov')
    appliance.coverage._install_simplecov()
    # Upload the merger
    logger.info('Installing coverage merger')
    appliance.coverage._upload_coverage_merger()
    with appliance.ssh_client as ssh:
        if not ssh.run_command('mkdir -p {}'.format(coverage_dir)):
            raise Exception(
                'Could not create coverage directory on the appliance: {}'.format(coverage_dir))

        # Download and extract all the coverage data
        for build_number in eligible_build_numbers:
            logger.info('Downloading the coverage data from build %s', build_number)
            download_url = jenkins_artifact_url(
                jenkins_user, jenkins_token, jenkins_url, job_name, build_number,
                artifacts['coverage-results.tgz']['relativePath'])
            cmd = ssh.run_command('curl -k -o {}/tmp.tgz {}'.format(
                coverage_dir,
                quote(download_url)))
            if cmd.failed:
                raise Exception('Could not download! - {}'.format(str(cmd)))

            # Extract coverage data
            logger.info('Extracting the coverage data from build %s', build_number)
            extract_command = ' && '.join([
                'cd {}'.format(coverage_dir),
                'tar xf tmp.tgz --strip-components=1',
                'rm -f tmp.tgz'])
            cmd = ssh.run_command(extract_command)
            if cmd.failed:
                raise Exception('Could not extract! - {}'.format(str(cmd)))

        merge_coverage_data(
            ssh=ssh,
            coverage_dir=coverage_dir)
        pull_merged_coverage_data(
            ssh=ssh,
            coverage_dir=coverage_dir)
        sonar_scan(
            ssh=ssh,
            project_name='CFME',
            project_version=str(appliance.version).strip(),
            scanner_url=sonar_scanner_url,
            scanner_dir=scanner_dir,
            server_url=sonar_server_url,
            timeout=scan_timeout)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Upload coverage data from jenkins job to sonarqube')
    parser.add_argument('jenkins_url')
    parser.add_argument('jenkins_job_name')
    parser.add_argument('work_appliance_ip')
    parser.add_argument('--jenkins-user', default=None)
    parser.add_argument('--jenkins-token', default=None)
    args = parser.parse_args()
    with IPAppliance(hostname=args.work_appliance_ip) as appliance:
        exit(main(
            appliance,
            args.jenkins_url,
            args.jenkins_user,
            args.jenkins_token,
            args.jenkins_job_name))

