#!/usr/bin/env python3

import argparse
import base64
import configparser
import getpass
import json
import os
import re
import sys
import urllib.request
import urllib.error
import urllib.parse

from collections import defaultdict, OrderedDict, namedtuple
from datetime import datetime
from string import Template


__location__ = os.path.realpath(os.path.join(os.getcwd(),
                                os.path.dirname(__file__)))
DEFAULT_CONFIG_FILE = os.path.join(__location__, 'config.ini')

config = defaultdict(dict)

# timestamp format for ISO-8601 timestamps in UTC
ISO_8601_UTC = '%Y-%m-%dT%H:%M:%SZ'

# Regular expression for matching issue cross-references in GitHub issue and
# comment text.  I can't find any documentation on GitHub as to what the
# allowed characters are in repositories and usernames, but this seems like a
# good-enough guess for now
GH_ISSUE_REF_RE = re.compile(r'(?:([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?)#'
                             r'([1-9]\d*)', flags=re.I)

# TODO: Do something useful with state management; my thought is to break this
# into actual stages of the import process where each stage will be in its own
# function.  A decorator could be used to identify what stage each function
# represents, and it should be possible to resume the import from any stage
# (where stages that have already been performed would be converted to no-ops)
class state:
    current = ""
    INITIALIZING         = "script-initializing"
    LOADING_CONFIG       = "loading-config"
    FETCHING_ISSUES      = "fetching-issues"
    GENERATING           = "generating"
    IMPORT_CONFIRMATION  = "import-confirmation"
    IMPORTING            = "importing"
    IMPORT_COMPLETE      = "import-complete"
    COMPLETE             = "script-complete"

state.current = state.INITIALIZING


HTTP_ERROR_MESSAGES = {
    401: "ERROR: There was a problem during authentication.\n"
         "Double check that your username and password are correct, and "
         "that you have permission to read from or write to the specified "
         "repositories.",
    404: "ERROR: Unable to find the specified repository.\n"
         "Double check the spelling for the source and target repositories. "
         "If either repository is private, make sure the specified user is "
         "allowed access to it."
}
# Basically the same problem. GitHub returns 403 instead to prevent abuse.
HTTP_ERROR_MESSAGES[403] = HTTP_ERROR_MESSAGES[401]


# Maps command-line options to their associated config file options (if any)
CONFIG_MAP = {
    'username': {'section': 'login', 'option': 'username'},
    'password': {'section': 'login', 'option': 'password'},
    'sources': {'section': 'global', 'option': 'sources', 'multiple': True},
    'target': {'section': 'global', 'option': 'target'},
    'ignore_comments': {'section': 'global', 'option': 'import-comments',
                        'negate': True},
    'ignore_milestone': {'section': 'global', 'option': 'import-milestone',
                         'negate': True},
    'ignore_labels': {'section': 'global', 'option': 'import-labels',
                      'negate': True},
    'ignore_assignee': {'section': 'global', 'option': 'import-assignee',
                        'negate': True},
    'no_backrefs': {'section': 'global', 'option': 'create-backrefs',
                    'negate': True},
    'close_issues': {'section': 'global', 'option': 'close-issues'},
    'import_issues': {'section': 'global', 'option': 'import-issues',
                      'multiple': True},
    'normalize_labels': {'section': 'global', 'option': 'normalize-labels'},
    'issue_template': {'section': 'format', 'option': 'issue-template'},
    'comment_template': {'section': 'format', 'option': 'comment-template'},
    'pull_request_template': {'section': 'format',
                              'option': 'comment-template'}
}


# Set of config option names that take boolean values; the options listed here
# can either be in the global section, or in per-repository sections
BOOLEAN_OPTS = set(['import-comments',  'import-milestone', 'import-labels',
                    'import-assignee', 'create-backrefs', 'close-issues',
                    'normalize-labels'])

class Issue(namedtuple('Issue', ('repository', 'number'))):
    """
    A namedtuple class representing a GitHub issue.  It has two fields: the
    repository name (as full username/repo pair) and the issue number as an
    int.
    """

    def __str__(self):
        return '%s#%s' % self


def init_config():
    """
    Handle command-line and config file processing; returns a `dict` of
    configuration combined from the config file and command-line options,
    as well as any default values.
    """

    config_defaults = {}

    conf_parser = argparse.ArgumentParser(add_help=False,
            description="Import issues from one GitHub repository into "
                        "another.")

    config_group = conf_parser.add_mutually_exclusive_group(required=False)
    config_group.add_argument('--config',
            help="The location of the config file (either absolute, or "
                 "relative to the current working directory). Defaults to "
                 "`config.ini` found in the same folder as this script.")

    config_group.add_argument('--no-config', dest='no_config',
            action='store_true',
            help="No config file will be used, and the default `config.ini` "
                 "will be ignored. Instead, all settings are either passed "
                 "as arguments, or (where possible) requested from the user "
                 "as a prompt.")

    arg_parser = argparse.ArgumentParser(parents=[conf_parser])

    arg_parser.add_argument('-u', '--username',
            help="The username of the account that will create the new "
                 "issues. The username will not be stored anywhere if "
                 "passed in as an argument.")

    arg_parser.add_argument('-p', '--password',
            help="The password (in plaintext) of the account that will "
                 "create the new issues. The password will not be stored "
                 "anywhere if passed in as an argument.")

    arg_parser.add_argument('-s', '--sources', nargs='+',
            help="The source repository or repositories from which the "
                 "issues should be copied.  If given more than one repository "
                 "the issues are merged from all repositories, and inserted "
                 "into the target repository in chronological order of their "
                 "creation.  Each repository should be in the format "
                 "`user/repository`.")

    arg_parser.add_argument('-t', '--target',
            help="The destination repository which the issues should be "
                 "copied to. Should be in the format `user/repository`.")

    arg_parser.add_argument('--ignore-comments', dest='ignore_comments',
            action='store_true', help="Do not import comments in the issue.")

    arg_parser.add_argument('--ignore-milestone', dest='ignore_milestone',
            action='store_true',
            help="Do not import the milestone attached to the issue.")

    arg_parser.add_argument('--ignore-labels', dest='ignore_labels',
            action='store_true',
            help="Do not import labels attached to the issue.")

    arg_parser.add_argument('--ignore-assignee', dest='ignore_assignee',
            action='store_true',
            help="Do not import the assignee to the issue.")

    arg_parser.add_argument('--no-backrefs', dest='no_backrefs',
            action='store_true',
            help="Do not reference original issues in migrated issues; "
                 "migrated issues will appear as though they were newly "
                 "created.")

    arg_parser.add_argument('--close-issues', dest='close_issues',
            action='store_true',
            help="Close original issues after they have been migrated.")

    arg_parser.add_argument('--issue-template',
            help="Specify a template file for use with issues.")

    arg_parser.add_argument('--comment-template',
            help="Specify a template file for use with comments.")

    arg_parser.add_argument('--pull-request-template',
            help="Specify a template file for use with pull requests.")

    arg_parser.add_argument('--normalize-labels', action='store_true',
            help="When creating new labels and merging with existing labels "
                 "normalize the label names by setting them to all lowercase "
                 "and replacing all whitespace with a single hyphen.")

    include_group = arg_parser.add_mutually_exclusive_group(required=True)
    include_group.add_argument('--all', dest='import_issues',
            action='store_const', const='all',
            help="Import all issues, regardless of state.")

    include_group.add_argument('--open', dest='import_issues',
            action='store_const', const='open',
            help="Import only open issues.")

    include_group.add_argument('--closed', dest='import_issues',
            action='store_const', const='closed',
            help="Import only closed issues.")

    include_group.add_argument('-i', '--issues', dest='import_issues',
            type=int, nargs='+', help="The list of issues to import.");


    # First parse arguments that affect reading the config files; use this to
    # set various defaults and then parse the remaining options
    conf_args, _ = conf_parser.parse_known_args()

    # TODO: This could be simplified even more with smarter use of argparse,
    # but good enough for now; it's not terribly important that this be
    # beautiful.

    if conf_args.no_config:
        print("Ignoring default config file. You may be prompted for some "
              "missing settings.")
    else:
        if conf_args.config:
            # Read default values out of the config file, if given--these
            # values may be overridden by command-line options
            config_file_name = conf_args.config
            if load_config_file(config_file_name):
                print("Loaded config options from '%s'" % config_file_name)
            else:
                sys.exit("ERROR: Unable to find or open config file '%s'" %
                         config_file_name)
        else:
            config_file_name = DEFAULT_CONFIG_FILE
            if load_config_file(config_file_name):
                print("Loaded options from default config file in '%s'" %
                      config_file_name)
            else:
                print("Default config file not found in '%s'" %
                      config_file_name)
                print("You may be prompted for some missing settings.")

        # Get global configuration defaults from 'global' and 'login', and
        # format sections of the config file
        for argname, config_map in CONFIG_MAP.items():
            section = config_map['section']
            option = config_map['option']

            val = config[section].get(option)
            if val is not None:
                if config_map.get('multiple'):
                    # A multiple-value can either be comma-separated or split
                    # across lines (but not both)
                    for sep in ('\n', ','):
                        if sep in val:
                            val = [v.strip() for v in val.split(sep)
                                   if v.strip()]
                            break
                    else:
                        val = [val]
                elif config_map.get('negate'):
                    val = not val
                config_defaults[argname] = val

    arg_parser.set_defaults(**config_defaults)

    args = arg_parser.parse_args()

    # Now load parsed args in to config dict; would be nice if there were a
    # better way to do this than to loop over CONFIG_MAP a second time.
    for argname, config_map in CONFIG_MAP.items():
        section = config_map['section']
        option = config_map['option']

        val = getattr(args, argname, None)
        if hasattr(args, argname) and val is not None:
            if config_map.get('multiple'):
                if not isinstance(val, list):
                    val = [val]
            elif config_map.get('negate'):
                val = not val
            config[section][option] = val

    # Make sure no required config values are missing
    sources = config['global'].get('sources')
    target = config['global'].get('target')

    if not sources:
        sys.exit("ERROR: There are no source repositories specified either in "
                 "the config file, or as a command-line argument.")
    if not target:
        sys.exit("ERROR: There is no target repository specified either in "
                 "the config file, or as an argument.")

    # GitHub seems to be case-insensitive wrt username/repository name, so
    # lowercase all repositories for consistency
    sources = config['global']['sources'] = [s.lower() for s in sources]
    target = config['global']['target'] = target.lower()

    for section in list(config):
        if section.startswith('repository:'):
            if section.lower() != section:
                config[section.lower()] = section
                del config[section]

    def get_server_for(repo):
        # Default to 'github.com' if no server is specified
        server = get_repository_option(repo, 'server')
        if server is None:
            server = 'github.com'
            set_repository_option(repo, 'server', 'github.com')

        # if SOURCE server is not github.com, then assume ENTERPRISE github
        # (yourdomain.com/api/v3...)
        if server == "github.com":
            api_url = "https://api.github.com"
        else:
            api_url = "https://%s/api/v3" % server

        set_repository_option(repo, 'url', '%s/repos/%s' % (api_url, repo))

    # Prompt for username/password if none is provided in either the config or an argument
    def get_credentials_for(repo):
        server = get_repository_option(repo, 'server')
        query_msg_1 = ("Do you wish to use the same credentials for the "
                       "target repository?")
        query_msg_2 = ("Enter your username for '%s' at '%s': " %
                       (repo, server))
        query_msg_3 = ("Enter your password for '%s' at '%s': " %
                       (repo, server))

        if get_repository_option(repo, 'username') is None:
            if config['login'].get('username'):
                username = config['login']['username']
            elif (repo == target and len(sources) == 1 and
                    yes_no(query_msg_1)):
                # One target and one source, where credentials for the target
                # were not supplied--ask to use the same credentials
                # TODO: In principle we could modify the logic here to take one
                # set of credentials and ask for each source *and* the target
                # repos to reuse those credentials, but for now this is just
                # reproducing the functionality that existed for single-source
                source = sources[0]
                username = get_repository_option(source, 'username')
            else:
                username = get_username(query_msg_2)

            set_repository_option(repo, 'username', username)

        if get_repository_option(repo, 'password') is None:
            # Again, support using the same password as the source, only if
            # there was a single source
            # TODO: Again, this logic could be modified to work better across
            # multiple sources, but it's not a priority right now.
            if config['login'].get('password'):
                password = config['login']['password']
            elif (repo == target and len(sources) == 1):
                source = sources[0]
                source_username = get_repository_option(source, 'username')
                source_server = get_repository_option(source, 'server')

                target_username = get_repository_option(repo, 'username')
                target_server = get_repository_option(repo, 'server')

                if (repo == target and
                        source_username == target_username and
                        source_server == target_server):
                    password = get_repository_option(source, 'password')
                else:
                    password = get_password(query_msg_3)
            else:
                password = get_password(query_msg_3)

            set_repository_option(repo, 'password', password)

    for repo in sources + [target]:
        get_server_for(repo)
        get_credentials_for(repo)

    # Everything is here! Continue on our merry way...


def load_config_file(config_file_name):
    global config  # global statement not strictly needed; just informational

    cfg = configparser.ConfigParser()
    try:
        with open(config_file_name) as f:
            cfg.read_file(f)

        for section in cfg.sections():
            for option in cfg.options(section):
                if ((section == 'global' or
                        section.startswith('repository:')) and
                     option in BOOLEAN_OPTS):
                    config[section][option] = cfg.getboolean(section, option)
                else:
                    config[section][option] = cfg.get(section, option)

        return True
    except (FileNotFoundError, IOError, configparser.Error):
        return False


def get_repository_option(repo, option, default=None):
    """
    Looks up per-repository options in the configuration; if not found it just
    returns the global setting from the [global] config section.

    Note, there are some repository-specific options (namely 'url') that should
    *only* appear in repository-specific config sections.
    """

    repo_sect = 'repository:' + repo
    if repo_sect in config and option in config[repo_sect]:
        section = repo_sect
    else:
        section = 'global'

    return config[section].get(option, default)


def set_repository_option(repo, option, value):
    """Sets a repository-specific option in the config."""

    config['repository:' + repo][option] = value


def normalize_label_name(label):
    """
    Lowercases a label name and replaces all whitespace with hyphens.
    """

    label = label.lower()
    return re.sub(r'\s+', '-', label)


def format_date(datestring):
    # The date comes from the API in ISO-8601 format
    date = datetime.strptime(datestring, ISO_8601_UTC)
    date_format = config['format'].get('date', '%A %b %d, %Y at %H:%M GMT')
    return date.strftime(date_format)


def format_from_template(template_filename, template_data):
    template_file = open(template_filename, 'r')
    template = Template(template_file.read())
    return template.substitute(template_data)


def format_issue(template_data):
    default_template = os.path.join(__location__, 'templates', 'issue.md')
    template = config['format'].get('issue-template', default_template)
    return format_from_template(template, template_data)


def format_pull_request(template_data):
    default_template = os.path.join(__location__, 'templates',
                                    'pull_request.md')
    template = config['format'].get('pull_request_template', default_template)
    return format_from_template(template, template_data)


def format_comment(template_data):
    default_template = os.path.join(__location__, 'templates', 'comment.md')
    template = config['format'].get('comment_template', default_template)
    return format_from_template(template, template_data)


def send_request(repo, url, post_data=None, method=None):
    if post_data is not None:
        post_data = json.dumps(post_data).encode("utf-8")

    repo_url = get_repository_option(repo, 'url')
    full_url = "%s/%s" % (repo_url, url)
    req = urllib.request.Request(full_url, post_data)

    if method is not None:
        req.method = method

    username = get_repository_option(repo, 'username')
    password = get_repository_option(repo, 'password')
    auth = base64.urlsafe_b64encode(
            ('%s:%s' % (username, password)).encode('utf-8'))
    req.add_header("Authorization", b'Basic ' + auth)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "spacetelescope/github-issues-import")

    try:
        response = urllib.request.urlopen(req)
        json_data = response.read()
    except urllib.error.HTTPError as error:

        error_details = error.read();
        error_details = json.loads(error_details.decode("utf-8"))

        if error.code in HTTP_ERROR_MESSAGES:
            sys.exit(HTTP_ERROR_MESSAGES[error.code])
        else:
            error_message = ("ERROR: There was a problem importing the "
                             "issues.\n%s %s" % (error.code, error.reason))
            if 'message' in error_details:
                error_message += "\nDETAILS: " + error_details['message']
            sys.exit(error_message)

    return json.loads(json_data.decode("utf-8"))


def get_milestones(repo):
    """
    Get all open milestones for repository.

    Returns an `OrderedDict` keyed on the milestone title.
    """

    milestones = send_request(repo, "milestones?state=open")
    return OrderedDict((m['title'], m) for m in milestones)


def get_labels(repo):
    """
    Get all labels for repository.

    Returns an `OrderedDict` keyed on label names.  If normalize-labels was
    specified in the configuration, this also normalizes all label names and
    ignores their original spellings.
    """

    normalize = get_repository_option(repo, 'normalize-labels')
    labels = send_request(repo, "labels")
    labels_dict = OrderedDict()
    for label in labels:
        if normalize:
            name = normalize_label_name(label['name'])
        else:
            name = label['name']

        labels_dict[name] = label
    return labels_dict


def get_issue_by_id(repo, issue_id):
    """Get single issue from repository."""

    issue = send_request(repo, "issues/%d" % issue_id)
    issue['repository'] = repo
    return issue


def get_issues_by_id(repo, issue_ids):
    """Get list of issues from repository for multiple issue numbers."""

    return [get_issue_by_id(repo, int(issue_id)) for issue_id in issue_ids]


def get_issues(repo, state=None):
    """
    Get all issues from repository.

    Optionally, only retrieve issues of in the specified state ('open' or
    'closed')."""

    issues = []
    page = 1
    while True:
        query_args = {'direction': 'asc', 'page': page}
        if state in ('open', 'closed'):
            query_args['state'] = state

        # TODO: Consider building this into send_request in the form of
        # optional kwargs or something
        query = urllib.parse.urlencode(query_args)
        new_issues = send_request(repo, 'issues?' + query)
        if not new_issues:
            break

        # Add a 'repository' key to each issue; although this information can
        # be gleaned from the issue data it's easier to include here explicitly
        for issue in new_issues:
            issue['repository'] = repo

        issues.extend(new_issues)
        page += 1
    return issues


def get_comments_on_issue(repo, issue):
    """Get all comments on an issue in the specified repository."""

    if issue['comments'] != 0:
        return send_request(repo, "issues/%s/comments" % issue['number'])
    else :
        return []


def import_milestone(source):
    data = {
        "title": source['title'],
        "state": "open",
        "description": source['description'],
        "due_on": source['due_on']
    }

    target = config['global']['target']
    result_milestone = send_request(target, "milestones", source)
    print("Successfully created milestone '%s'" % result_milestone['title'])
    return result_milestone


def import_label(source):
    data = {
        "name": source['name'],
        "color": source['color']
    }

    target = config['global']['target']
    result_label = send_request(target, "labels", source)
    print("Successfully created label '%s'" % result_label['name'])
    return result_label


def import_comments(comments, issue_number):
    result_comments = []
    for comment in comments:

        template_data = {}
        template_data['user_name'] = comment['user']['login']
        template_data['user_url'] = comment['user']['html_url']
        template_data['user_avatar'] = comment['user']['avatar_url']
        template_data['date'] = format_date(comment['created_at'])
        template_data['url'] =  comment['html_url']
        template_data['body'] = comment['body']

        comment['body'] = format_comment(template_data)

        target = config['global']['target']
        result_comment = send_request(target, "issues/%s/comments" %
                                      issue_number, comment)
        result_comments.append(result_comment)

    return result_comments


def fixup_cross_references(source_repo, issue, issue_map):
    """
    Before inserting new issues into the target repository, this checks the
    original issue body for references to other issues in the original
    repository *or* issues in any of the other source repositories being
    migrated from.

    This can't reasonably update every existing reference to the original
    issue, but it can ensure that all issue cross-references made in the new
    issue are internally consistent.
    """

    # TODO: This should be done for cross-references in comments as well.
    def repl_issue_reference(matchobj):
        """
        If a matched issue reference is to within the same repository,
        it is updated to explictly reference the source repository (rather
        than a 'bare' issue reference like '#42').  However, if the referenced
        issue is one of the other issues being migrated, then it updates the
        reference to point to the newly migrated issue.
        """

        repo = matchobj.group(1) or source_repo
        issue_num = int(matchobj.group(2))

        issue = Issue(repo, issue_num)

        if issue in issue_map:
            # Update to reference another issue being migrated to the target
            # repository
            return '#' + issue_map[issue][1]
        else:
            return str(issue)

    issue['body'] = GH_ISSUE_REF_RE.sub(repl_issue_reference, issue['body'])


# Will only import milestones and issues that are in use by the imported
# issues, and do not exist in the target repository
def import_issues(issues, issue_map):
    state.current = state.GENERATING

    target = config['global']['target']
    known_milestones = get_milestones(target)
    known_labels = get_labels(target)

    new_issues = []
    skipped_issues = OrderedDict()
    num_new_comments = 0
    new_milestones = []
    new_labels = []

    for issue, old_issue in zip(issues, issue_map):
        repo = issue['repository']

        if issue['migrated']:
            skipped_issues[repo, issue['number']] = issue
            continue

        new_issue = {}
        new_issue['title'] = issue['title']

        # Temporary fix for marking closed issues
        if issue['closed_at']:
            new_issue['title'] = "[CLOSED] " + new_issue['title']

        import_assignee = get_repository_option(repo, 'import-assignee')
        if import_assignee and issue.get('assignee'):
            new_issue['assignee'] = issue['assignee']['login']

        num_comments = int(issue.get('comments', 0))
        if (get_repository_option(repo, 'import-comments') and
                num_comments != 0):
            num_new_comments += num_comments
            new_issue['comments'] = get_comments_on_issue(repo, issue)

        import_milestone = get_repository_option(repo, 'import-milestone')
        if import_milestone and issue.get('milestone') is not None:
            # Since the milestones' ids are going to differ, we will compare
            # them by title instead
            milestone_title = issue['milestone']['title']
            found_milestone = known_milestones.get(milestone_title)
            if found_milestone:
                new_issue['milestone_object'] = found_milestone
            else:
                new_milestone = issue['milestone']
                new_issue['milestone_object'] = new_milestone
                # Allow it to be found next time
                known_milestones[new_milestone['title']] = new_milestone
                # Put it in a queue to add it later
                new_milestones.append(new_milestone)

        import_labels = get_repository_option(repo, 'import-labels')
        normalize_labels = get_repository_option(repo, 'normalize-labels')
        if import_labels and issue.get('labels') is not None:
            new_issue['label_objects'] = []
            for issue_label in issue['labels']:
                if normalize_labels:
                    issue_label['name'] = \
                            normalize_label_name(issue_label['name'])
                found_label = known_labels.get(issue_label['name'])
                if found_label:
                    new_issue['label_objects'].append(found_label)
                else:
                    new_issue['label_objects'].append(issue_label)
                    # Allow it to be found next time
                    known_labels[issue_label['name']] = issue_label
                    # Put it in a queue to add it later
                    new_labels.append(issue_label)

        fixup_cross_references(repo, issue, issue_map)

        template_data = {}
        template_data['user_name'] = issue['user']['login']
        template_data['user_url'] = issue['user']['html_url']
        template_data['user_avatar'] = issue['user']['avatar_url']
        template_data['date'] = format_date(issue['created_at'])
        template_data['url'] =  issue['html_url']
        template_data['body'] = issue['body']
        template_data['num_comments'] = num_comments

        if get_repository_option(repo, 'create-backrefs'):
            if ("pull_request" in issue and
                    issue['pull_request']['html_url'] is not None):
                new_issue['body'] = format_pull_request(template_data)
            else:
                new_issue['body'] = format_issue(template_data)
        else:
            new_issue['body'] = issue['body']

        new_issues.append((old_issue, new_issue))

    state.current = state.IMPORT_CONFIRMATION

    print("You are about to add to '%s':" % target)
    print(" *", len(new_issues), "new issues:")

    for old, new in issue_map.items():
        if old in skipped_issues:
            continue

        print("   *", old, "->", new)

    print(" *", num_new_comments, "new comments")
    print(" *", len(new_milestones), "new milestones")
    print(" *", len(new_labels), "new labels")

    if skipped_issues:
        print(" *", "The following issues look like they have already been "
                    "migrated to the target repository by this script and "
                    "will not be migrated:")
        for key, issue in skipped_issues.items():
            print ("   *", key)

    if not yes_no("Are you sure you wish to continue?"):
        sys.exit()

    state.current = state.IMPORTING

    for milestone in new_milestones:
        result_milestone = import_milestone(milestone)
        milestone['number'] = result_milestone['number']
        milestone['url'] = result_milestone['url']

    for label in new_labels:
        result_label = import_label(label)

    result_issues = []
    for old_issue, issue in new_issues:
        if 'milestone_object' in issue:
            issue['milestone'] = issue['milestone_object']['number']
            del issue['milestone_object']

        if 'label_objects' in issue:
            issue_labels = []
            for label in issue['label_objects']:
                issue_labels.append(label['name'])
            issue['labels'] = issue_labels
            del issue['label_objects']

        result_issue = send_request(target, "issues", issue)

        source_repo, number = old_issue
        close_issue = get_repository_option(source_repo, 'close-issues')

        if close_issue:
            close_message = '; the original issue will be closed'
        else:
            close_message = ''

        print("Successfully created issue '%s'%s" % (result_issue['title'],
                                                     close_message))

        # Now update the original issue to mention the new issue.
        update = {}

        if get_repository_option(source_repo, 'create-backrefs'):
            orig_issue = get_issue_by_id(source_repo, int(number))
            message = (
                '*Migrated to %s#%s by [spacetelescope/github-issues-import]'
                '(https://github.com/spacetelescope/github-issues-import)*' %
                (target, result_issue['number']))
            update['body'] = message + '\n\n' + orig_issue['body']

        if close_issue:
            update['state'] = 'closed'

        send_request(source_repo, 'issues/%s' % number, update, 'PATCH')
        print("Updated original issue with mapping from %s -> %s" %
              (old_issue, issue_map[old_issue]))

        if 'comments' in issue:
            result_comments = import_comments(issue['comments'],
                                              result_issue['number'])
            print(" > Successfully added", len(result_comments), "comments.")

        result_issues.append(result_issue)

    state.current = state.IMPORT_COMPLETE

    return result_issues


def get_username(question):
    # Reserve this are in case I want to prevent special characters etc in the future
    return input(question)


def get_password(question):
    return getpass.getpass(question)


# Taken from http://code.activestate.com/recipes/577058-query-yesno/
#  with some personal modifications
def yes_no(question, default=True):
    choices = {"yes":True, "y":True, "ye":True,
               "no":False, "n":False }

    if default == None:
        prompt = " [y/n] "
    elif default == True:
        prompt = " [Y/n] "
    elif default == False:
        prompt = " [y/N] "
    else:
        raise ValueError("invalid default answer: '%s'" % default)

    while 1:
        sys.stdout.write(question + prompt)
        choice = input().lower()
        if default is not None and choice == '':
            return default
        elif choice in choices.keys():
            return choices[choice]
        else:
            sys.stdout.write("Please respond with 'yes' or 'no' "\
                             "(or 'y' or 'n').\n")


if __name__ == '__main__':

    state.current = state.LOADING_CONFIG

    init_config()

    state.current = state.FETCHING_ISSUES

    # Argparser will prevent us from getting both issue ids and specifying
    # issue state, so no duplicates will be added
    issues = []
    for repo in config['global']['sources']:
        issues_to_import = get_repository_option(repo, 'import-issues')

        if (len(issues_to_import) == 1 and
                issues_to_import[0] in ('all', 'open', 'closed')):
            issues += get_issues(repo, state=issues_to_import[0])
        else:
            issues += get_issues_by_id(repo, issues_to_import)

    # Sort issues from all repositories chronologically
    issues.sort(key=lambda i: datetime.strptime(i['created_at'],
                                                ISO_8601_UTC))

    target = config['global']['target']

    migrated_re = re.compile(
            r'^\*Migrated to (%s)#(\d+) by.*'
            r'spacetelescope/github-issues-import' % target)

    # Determine if any of the found issues have already been migrated and mark
    # them if such.  Already migrated issues will be ignored unless the
    # update-existing option is set.

    def issue_was_migrated(issue):
        """
        Determine if the issue looks like it has already been migrated by this
        script.

        If the issue was migrated, it returns an `Issue` object representing
        its migration destination; returns `False` otherwise.
        """

        for line in issue['body'].splitlines():
            m = migrated_re.match(line)
            if m:
                return Issue(m.group(1), int(m.group(2)))

        return False

    # Get all issues in the target repository; obviously if issues are created
    # in the target repo before the script is finished running this list will
    # be inaccurate; later we will warn the user to lock down the target (and
    # source) repos before merging in order to prevent this
    # TODO: I wonder if this lockdown could actually be done via the API?
    target_issues = get_issues(target)
    # Annoyingly, the GitHub API does not have a way to ask for a simple count
    # of issues; instead we have to download all the issues in full in order to
    # count them

    # Create a map from issues in the source repositories to the issues they
    # will become in the new repository
    new_issue_idx = len(target_issues) + 1
    issue_map = OrderedDict()
    for issue in issues:
        migrated = issue['migrated'] = issue_was_migrated(issue)
        old = Issue(issue['repository'], issue['number'])
        if migrated:
            new = migrated
        else:
            new = Issue(target, new_issue_idx)
            new_issue_idx += 1

        issue_map[old] = new

    # Further states defined within the function
    # Finally, add these issues to the target repository
    import_issues(issues, issue_map)

    state.current = state.COMPLETE
