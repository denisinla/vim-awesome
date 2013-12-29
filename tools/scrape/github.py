import base64
import collections
import datetime
import re
import sys
import time
from urllib import urlencode
import urlparse

import dateutil.parser
import requests
import rethinkdb as r
from termcolor import cprint

from db.github_repos import PluginGithubRepos, DotfilesGithubRepos
import db.util
import tools.scrape.db_upsert as db_upsert
import util

r_conn = db.util.r_conn

try:
    import secrets
    _GITHUB_API_TOKEN = getattr(secrets, 'GITHUB_PERSONAL_ACCESS_TOKEN', None)
except ImportError:
    _GITHUB_API_TOKEN = None


_NO_GITHUB_API_TOKEN_MESSAGE = """
*******************************************************************************
* Warning: GitHub API token not found in secrets.py. Scraping will be severely
* rate-limited. See secrets.py.example to obtain a GitHub personal access token
*******************************************************************************
"""
if not _GITHUB_API_TOKEN:
    cprint(_NO_GITHUB_API_TOKEN_MESSAGE, 'red')


# The following are names of repos and locations where we search for
# Vundle/Pathogen plugin references. They were found by manually going through
# search results of
# github.com/search?q=scrooloose%2Fsyntastic&ref=searchresults&type=Code

# TODO(david): It would be good to add "vim", "settings", and "config", but
#     there are too many false positives that need to be filtered out.
_DOTFILE_REPO_NAMES = ['dotfile', 'vimrc', 'vimfile', 'vim-file', 'vimconf',
        'vim-conf', 'dotvim', 'config-files', 'vim-setting', 'myvim']

_VIMRC_FILENAMES = ['vimrc', 'bundle', 'vundle.vim', 'vundles.vim',
        'vim.config', 'plugins.vim']

_VIM_DIRECTORIES = ['vim', 'config', 'home']


# Regexes for extracting Vundle plugin references from dotfile repos. See
# github_test.py for examples of what they match and don't match.

# Matches eg. "Bundle 'gmarik/vundle'" or "Bundle 'taglist'"
# [^\S\n] means whitespace except newline: stackoverflow.com/a/3469155/392426
_BUNDLE_PLUGIN_REGEX_TEMPLATE = r'^[^\S\n]*%s[^\S\n]*[\'"]([^\'"\n\r]+)[\'"]'
_VUNDLE_PLUGIN_REGEX = re.compile(_BUNDLE_PLUGIN_REGEX_TEMPLATE % 'Bundle',
        re.MULTILINE)
_NEOBUNDLE_PLUGIN_REGEX = re.compile( _BUNDLE_PLUGIN_REGEX_TEMPLATE %
        '(?:NeoBundle|NeoBundleFetch|NeoBundleLazy)', re.MULTILINE)

# Extracts ('gmarik', 'vundle') or (None, 'taglist') from the above examples.
_BUNDLE_OWNER_REPO_REGEX = re.compile(
        r'(?:([^:\'"/]+)/)?([^\'"\n\r/]+?)(?:\.git)?$')


def get_api_page(url_or_path, query_params=None, page=1, per_page=100):
    """Get a page from GitHub's v3 API.

    Arguments:
        url_or_path: The API method to call or the full URL.
        query_params: A dict of additional query parameters
        page: Page number
        per_page: How many results to return per page. Max is 100.

    Returns:
        A tuple: (Response object, JSON-decoded dict of the response)
    """
    split_url = urlparse.urlsplit(url_or_path)

    query = {
        'page': page,
        'per_page': per_page,
    }

    if _GITHUB_API_TOKEN:
        query['access_token'] = _GITHUB_API_TOKEN

    query.update(dict(urlparse.parse_qsl(split_url.query)))
    query.update(query_params or {})

    url = urlparse.SplitResult(scheme='https', netloc='api.github.com',
            path=split_url.path, query=urlencode(query),
            fragment=split_url.fragment).geturl()

    res = requests.get(url)
    return res, res.json()


def maybe_wait_until_api_limit_resets(response_headers):
    """If we're about to exceed our API limit, sleeps until our API limit is
    reset.
    """
    if response_headers['X-RateLimit-Remaining'] == '0':
        reset_timestamp = response_headers['X-RateLimit-Reset']
        reset_date = datetime.datetime.fromtimestamp(int(reset_timestamp))
        now = datetime.datetime.now()
        seconds_to_wait = (reset_date - now).seconds + 1
        print "Sleeping %s seconds for API limit to reset." % seconds_to_wait
        time.sleep(seconds_to_wait)


def fetch_plugin(owner, repo_name, repo_data=None, readme_data=None):
    """Fetch a plugin from a github repo"""
    if not repo_data:
        res, repo_data = get_api_page('repos/%s/%s' % (owner, repo_name))
        if res.status_code == 404:
            return None, repo_data

    if not readme_data:
        _, readme_data = get_api_page('repos/%s/%s/readme' % (
            owner, repo_name))

    readme_base64_decoded = base64.b64decode(readme_data.get('content', ''))
    readme = unicode(readme_base64_decoded, 'utf-8', errors='ignore')

    vim_script_id = None
    homepage = repo_data['homepage']

    if homepage and homepage.startswith('http://www.vim.org/scripts/'):
        vim_script_url = homepage
        match = re.search('script_id=(\d+)', vim_script_url)
        if match:
            vim_script_id = int(match.group(1))

    repo_created_date = dateutil.parser.parse(repo_data['created_at'])

    # Fetch commits so we can get the update/create dates.
    _, commits_data = get_api_page('repos/%s/%s/commits' % (owner, repo_name),
            per_page=100)

    if commits_data and isinstance(commits_data, list) and len(commits_data):

        # Unfortunately repo_data['updated_at'] and repo_data['pushed_at'] are
        # wildy misrepresentative of the last time someone made a commit to the
        # repo.
        updated_date_text = commits_data[0]['commit']['author']['date']
        updated_date = dateutil.parser.parse(updated_date_text)

        # To get the creation date, we use the heuristic of min(repo creation
        # date, 100th latest commit date). We do this because repo creation
        # date can be later than the date of the first commit, which is
        # particularly pervasive for vim-scripts repos. Fortunately, most
        # vim-scripts repos don't have more than 100 commits, and also we get
        # creation_date for vim-scripts repos when scraping vim.org.
        early_commit_date_text = commits_data[-1]['commit']['author']['date']
        early_commit_date = dateutil.parser.parse(early_commit_date_text)
        created_date = min(repo_created_date, early_commit_date)

    else:
        updated_date = dateutil.parser.parse(repo_data['updated_at'])
        created_date = repo_created_date

    # Fetch owner info to get author name.
    owner_login = repo_data['owner']['login']
    if owner_login == 'vim-scripts':
        author = None
    else:
        _, owner_data = get_api_page('users/%s' % owner_login)
        author = owner_data.get('name') or owner_data.get('login')

    repo = {
        'name': repo_name,
        'github_url': repo_data['html_url'],
        'vim_script_id': vim_script_id,
        'homepage': homepage,
        'github_stars': repo_data['watchers'],
        'github_short_desc': repo_data['description'],
        'github_readme': readme,
        'created_at': util.to_timestamp(created_date),
        'updated_at': util.to_timestamp(updated_date),
    }

    if author:
        repo['author'] = author

    return (repo, repo_data)


def get_requests_left():
    """Retrieve how many API requests are remaining"""
    _, data = get_api_page('rate_limit')

    return data['rate']['remaining']


def scrape_plugin_repos(num):
    """Scrapes the num plugin repos that have been least recently scraped."""
    query = r.table('plugin_github_repos').filter({'is_blacklisted': False})
    query = query.order_by('last_scraped_at').limit(num)
    repos = query.run(r_conn())

    # TODO(david): Print stats at the end: # successfully scraped, # not found,
    #     # redirects, etc.
    for repo in repos:
        repo_name = repo['repo_name']
        repo_owner = repo['owner']

        # Print w/o newline.
        print "    scraping %s/%s ..." % (repo_owner, repo_name),
        sys.stdout.flush()

        # TODO(david): One optimization is to pass in repo['repo_data'] for
        #     vim-scripts repos (since we already save that when discovering
        #     vim-scripts repos in build_github_index.py). But the
        #     implementation here should not be coupled with implemenation
        #     details in build_github_index.py.
        plugin, repo_data = fetch_plugin(repo_owner, repo_name)

        repo['repo_data'] = repo_data
        PluginGithubRepos.log_scrape(repo)
        r.table('plugin_github_repos').insert(repo, upsert=True).run(r_conn())

        if plugin:

            # If this plugin's repo was mentioned in vim.org script
            # descriptions, try to see if this plugin matches any of those
            # scripts before a global search.
            query_filter = None
            if repo.get('from_vim_scripts'):
                vim_script_ids = repo['from_vim_scripts']
                query_filter = (lambda plugin:
                        plugin['vim_script_id'] in vim_script_ids)

            # TODO(david): We should probably still wrap this in a try block.
            db_upsert.upsert_plugin(plugin, query_filter)

            print "done"

        else:
            # TODO(david): Insert some metadata in the github repo that this is
            #     not found
            print "not found."
            continue


def _extract_bundles_with_regex(file_contents, bundle_plugin_regex):
    """Extracts plugin repos from contents of a file using a given regex.

    Arguments:
        file_contents: A string of the contents of the file to search through.
        bundle_plugin_regex: A regex to use to match all lines referencing
            plugin repos.

    Returns:
        A list of tuples (owner, repo_name) referencing GitHub repos.
    """
    bundles = bundle_plugin_regex.findall(file_contents)
    if not bundles:
        return []

    plugin_repos = []
    for bundle in bundles:
        match = _BUNDLE_OWNER_REPO_REGEX.search(bundle)
        if match and len(match.groups()) == 2:
            owner, repo = match.groups()
            owner = 'vim-scripts' if owner is None else owner
            plugin_repos.append((owner, repo))
        else:
            cprint('Failed to extract owner/repo from "%s"' % bundle, 'red')

    return plugin_repos


def _extract_bundle_repos_from_file(file_contents):
    """Extracts Vundle and Neobundle plugins from contents of a vimrc-like
    file.

    Arguments:
        file_contents: A string of the contents of the file to search through.

    Returns:
        A tuple (Vundle repos, NeoBundle repos). Each element is a list of
        tuples of the form (owner, repo_name) referencing a GitHub repo.
    """
    vundle_repos = _extract_bundles_with_regex(file_contents,
            _VUNDLE_PLUGIN_REGEX)
    neobundle_repos = _extract_bundles_with_regex(file_contents,
            _NEOBUNDLE_PLUGIN_REGEX)

    return vundle_repos, neobundle_repos


def _extract_bundle_repos_from_dir(dir_data, depth=0):
    """Extracts vim plugin bundles from a GitHub dotfiles directory.

    Will recursively search through directories likely to contain vim config
    files (lots of people seem to like putting their vim config in a "vim"
    subdirectory).

    Arguments:
        dir_data: API response from GitHub of a directory or repo's contents.
        depth: Current recursion depth (0 = top-level repo).

    Returns:
        A tuple (Vundle repos, NeoBundle repos). Each element is a list of
        tuples of the form (owner, repo_name) referencing a GitHub repo.
    """
    if depth >= 3:
        return

    # First, look for top-level files that are likely to contain references to
    # vim plugins.
    files = filter(lambda f: f['type'] == 'file', dir_data)
    for file_data in files:
        filename = file_data['name'].lower()

        if 'gvimrc' in filename:
            continue

        if not any((f in filename) for f in _VIMRC_FILENAMES):
            continue

        # Ok, there could potentially be references to vim plugins here.
        _, file_contents = get_api_page(file_data['url'])
        contents_decoded = base64.b64decode(file_contents.get('content', ''))
        bundles_tuple = _extract_bundle_repos_from_file(contents_decoded)

        if any(bundles_tuple):
            return bundles_tuple

    # No plugins were found, so look in subdirectories that could potentially
    # have vim config files.
    dirs = filter(lambda f: f['type'] == 'dir', dir_data)
    for dir_data in dirs:
        filename = dir_data['name'].lower()
        if not any((f in filename) for f in _VIM_DIRECTORIES):
            continue

        # Ok, there could potentially be vim config files in here.
        _, subdir_data = get_api_page(dir_data['url'])
        bundles_tuple = _extract_bundle_repos_from_dir(subdir_data, depth + 1)

        if any(bundles_tuple):
            return bundles_tuple

    return [], []


def _extract_pathogen_repos(repo_contents):
    return []  # TODO(david)


def _get_plugin_repos_from_dotfiles(repo_data, search_keyword):
    """Search for references to vim plugin repos from a dotfiles repository,
    and insert them into DB.

    Arguments:
        repo_data: API response from GitHub of a repository.
        search_keyword: The keyword used that found this repo.
    """
    owner_repo = repo_data['full_name']

    # Print w/o newline.
    print "    scraping %s ..." % owner_repo,
    sys.stdout.flush()

    res, contents_data = get_api_page('repos/%s/contents' % owner_repo)

    if res.status_code == 404 or not isinstance(contents_data, list):
        print "contents not found"
        return

    vundle_repos, neobundle_repos = _extract_bundle_repos_from_dir(
            contents_data)
    pathogen_repos = _extract_pathogen_repos(contents_data)

    owner, repo_name = owner_repo.split('/')
    db_repo = DotfilesGithubRepos.get_with_owner_repo(owner, repo_name)
    pushed_date = dateutil.parser.parse(repo_data['pushed_at'])

    def stringify_repo(owner_repo_tuple):
        return '/'.join(owner_repo_tuple)

    repo = dict(db_repo or {}, **{
        'owner': owner,
        'pushed_at': util.to_timestamp(pushed_date),
        'repo_name': repo_name,
        'search_keyword': search_keyword,
        'vundle_repos': map(stringify_repo, vundle_repos),
        'neobundle_repos': map(stringify_repo, neobundle_repos),
        'pathogen_repos': map(stringify_repo, pathogen_repos),
    })

    DotfilesGithubRepos.log_scrape(repo)
    DotfilesGithubRepos.upsert_with_owner_repo(repo)

    print 'found %s Vundles, %s NeoBundles, %s Pathogens' % (
            len(vundle_repos), len(neobundle_repos), len(pathogen_repos))

    return {
        'vundle_repos_count': len(vundle_repos),
        'neobundle_repos_count': len(neobundle_repos),
        'pathogen_repos_count': len(pathogen_repos),
    }


def scrape_dotfiles_repos(num):
    """Scrape at most num dotfiles repos from GitHub for references to Vim
    plugin repos.

    We perform a search on GitHub repositories that are likely to contain
    Vundle and Pathogen bundles instead of a code search matching
    Vundle/Pathogen commands (which has higher precision and recall), because
    GitHub's API requires code search to be limited to
    a user/repo/organization. :(
    """
    # Earliest allowable updated date to start scraping from (so we won't be
    # scraping repos that were last pushed before this date).
    EARLIEST_PUSHED_DATE = datetime.datetime(2013, 1, 1)

    repos_scraped = 0
    scraped_counter = collections.Counter()

    for repo_name in _DOTFILE_REPO_NAMES:
        latest_repo = DotfilesGithubRepos.get_latest_with_keyword(repo_name)

        if latest_repo and latest_repo.get('pushed_at'):
            last_pushed_date = max(datetime.datetime.utcfromtimestamp(
                    latest_repo['pushed_at']), EARLIEST_PUSHED_DATE)
        else:
            last_pushed_date = EARLIEST_PUSHED_DATE

        # We're going to scrape all repos updated after the latest updated repo
        # in our DB, starting with the least recently updated.  This maintains
        # the invariant that we have scraped all repos pushed before the latest
        # push date (and after EARLIEST_PUSHED_DATE).
        while True:

            start_date_iso = last_pushed_date.isoformat()
            search_params = {
                'q': '%s in:name pushed:>%s' % (repo_name, start_date_iso),
                'sort': 'updated',
                'order': 'asc',
            }

            per_page = 100
            response, search_data = get_api_page('search/repositories',
                    query_params=search_params, page=1, per_page=per_page)

            items = search_data.get('items', [])
            for item in items:
                stats = _get_plugin_repos_from_dotfiles(item, repo_name)
                scraped_counter.update(stats)

                # If we've scraped the number repos desired, we can quit.
                repos_scraped += 1
                if repos_scraped >= num:
                    return repos_scraped, scraped_counter

            # If we're about to exceed the rate limit (20 requests / min),
            # sleep until the limit resets.
            maybe_wait_until_api_limit_resets(response.headers)

            # If we've scraped all repos with this name, move on to the next
            # repo name.
            if len(items) < per_page:
                break
            else:
                last_pushed_date = dateutil.parser.parse(
                        items[-1]['pushed_at'])

    return repos_scraped, scraped_counter
