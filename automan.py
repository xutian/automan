import os
import re
import sys
import json
import urllib2
import argparse
from lxml import html
from bugzilla import Bugzilla
from libsaas.services import github
from ConfigParser import SafeConfigParser


def apilink2htmlink(apiurl):
    try:
        request = urllib2.urlopen(apiurl)
        html = request.read()
        info = json.loads(html)
        return str(info["html_url"])
    except Exception as details:
        print "Error: %s" % details
    return None


def send_request(resource, conditions=None):
    out = []
    kwargs = {"page": 1, "per_page": 50}
    if conditions:
        kwargs.update(conditions)
    while True:
        objs = resource.get(**kwargs)
        out += objs
        kwargs["page"] += 1
        if len(objs) < kwargs["per_page"]:
            break
    return out


def get_issues(repo, state='all'):
    """
    Get pull repuest list by state
    """
    request = repo.pullrequests()
    # FixMe:
    #    how to list all pullrequest?
    kwargs = {"state": state}
    return send_request(request, kwargs)


def get_merged(repo):
    """
    Get merged pull request in repo
    """
    out = []
    for info in get_issues(repo, "closed"):
        issue = repo.pullrequest(info["number"])
        if issue.is_merged():
            out.append(info["number"])
    return out


def resolved_bugs(repo, num):
    """
    Get bugs ID from issue comments
    :param issue: libsaas github issue object
    """
    out = []
    issue = repo.issue(num)
    handler = issue.comments()
    comments = send_request(handler)
    pullreq = repo.pullrequest(num)
    comment0 = pullreq.get()["body"]
    comment0 = {"body": comment0}
    comments.insert(0, comment0)
    regex = re.compile(r"id:(.*)", re.M | re.I)
    for comment in comments:
        text = comment.get("body", "")
        text = text.encode("utf-8")
        match = regex.search(text)
        if match:
            out += re.findall(r"\d+", match.groups()[0])
    return out


def requried_fixs(bug):
    """
    Search bug comments to find out which fix are requried.
    :param bug: bugzilla bug object.
    """
    out = []
    reg = re.compile(r"(http.*/[a-z]+/\d+/?$)", re.M | re.I)
    for comment in bug.getcomments():
        text = comment.get("text")
        out += reg.findall(text)
    return set(map(lambda x: x.rstrip('/'), out))


def dispatch_ghlink(link):
    """
    Dispatch URL link to github pullreq

    :param link: url of github pull-request
    :return set: github user, name and issue ID
    """
    partten = r"http.*/(.*)/(.*)/pull/(\d+)"
    regex = re.compile(partten, re.M | re.I)
    match = regex.search(link)
    if match:
        return match.groups()
    return None


def add_gh_link(bug, pullreq):
    """
    Comment github pullrequest link to related bug

    :param bug: bugzilla Bug object
    :param pullreq: github pullrequest object
    """
    api_url = pullreq.get_url()
    html_url = apilink2htmlink(api_url)
    comments = bug.getcomments()
    for comment in comments:
        if html_url in comment.get("text"):
            return
    comment = ("Fix has submited, follow below link"
               " to see details.\n\t%s" % html_url)
    bug.addcomment(comment)
    print "INFO - add %s to bug %s" % (comment, bug.bug_id)


def get_phstate(url):
    """
    Get patch status from html source, soulation is not
    good enough, it depends on html style.

    :param url: url of patch in patchwork
    """
    xpath = '//table[@class="patchmeta"][1]/tr'
    try:
        handler = html.parse(url)
        for tr in handler.xpath(xpath):
            for el in tr.getchildren():
                if el.text and "State" in el.text:
                    return el.getnext().text.strip()
    except Exception as details:
        print "Error - %s" % details
    return None


def is_ghlink(link):
    pattern = r"http.*/pull/\d+/?"
    return re.match(pattern, link)


def is_phlink(link):
    pattern = r"http.*/patch/\d+/?"
    return re.match(pattern, link)


def link2pullreq(link, gh):
    info = dispatch_ghlink(link)
    try:
        repo = gh.repo(info[0], info[1])
        return repo.pullrequest(info[2])
    except Exception:
        print "ERROR - Invaild pullreq link '%s'" % link
    return None


def is_merged(pullreq):
    return pullreq.is_merged()


def is_applied(link):
    state = get_phstate(link)
    return state == "Accepted"


def is_ready4qa(bug, gh):
    """
    Check is bug ready for set status to ON_QA.

    :param bug: bugzilla bug object.
    :return bool: True or False
    """
    # If no fix found in bugzilla, skip go through github
    # to find pullrequest.
    links = requried_fixs(bug)
    if not links:
        return False

    # No need to set comments and bug status for bug in
    # below status.
    if bug.status in ["VERIFIED", "CLOSED", "ON_QA"]:
        return False

    ghlinks = filter(is_ghlink, links)
    phlinks = filter(is_phlink, links)
    pullreqs = map(lambda link: link2pullreq(link, gh), ghlinks)
    pullreqs = filter(None, pullreqs)
    notappliedphs = filter(lambda x: not is_applied(x), phlinks)
    notmergedghs = filter(lambda x: not is_merged(x), pullreqs)
    for pullreq in notmergedghs:
        ghlink = apilink2htmlink(pullreq.get_url())
        if ghlink:
            print "INFO: %s not merge" % ghlink
    for phlink in notappliedphs:
        print "INFO: %s not applied" % phlink
    return not (notmergedghs or notappliedphs)


def move_state2onqa(bug):
    """
    Move bug status to ON_QA.
    """
    print "INFO: - Bug %s ready for merged" % bug.bug_id
    comment = ("All patch/pullrequest applied, "
               "so move state to ON_QA. "
               "If you still meet the defeat, "
               "please reopen it.\n\nThanks,\nXu")
    try:
        return bug.setstatus(status="ON_QA", comment=comment)
    except Exception as details:
        print ("Error - Update bug %s status failed(%s)" %
               (bug.bug_id, details))
    return None


def get_ghlink2bugs(bzla, gh, ghuser, ghname):
    bugs = []
    repo = gh.repo(ghuser, ghname)
    nums = get_merged(repo)
    for num in nums:
        pullreq = repo.pullrequest(num)
        for bzid in resolved_bugs(repo, num):
            print "INFO - Checking bug %s" % bzid
            bug = bzla.getbug(bzid)
            if bug.status in ["ON_QA", "VERIFIED", "CLOSED"]:
                continue
            add_gh_link(bug, pullreq)
            bugs.append(bug)
    return set(bugs)


def get_bugzilla(parse):
    print "INFO - Connecting to bugzilla"
    try:
        bzusername = parse.get("bugzilla", "username")
        bzpassword = parse.get("bugzilla", "password")
        bzrpcurl = parse.get("bugzilla", "rpcurl")
        return Bugzilla(url=bzrpcurl, user=bzusername, password=bzpassword)
    except Exception as details:
        print "Error - get bugzilla connection failed: %s" % details


def get_github(parse):
    print "INFO - Connecting to github"
    try:
        ghusername = parse.get("github", "username")
        ghpassword = parse.get("github", "password")
        return github.GitHub(ghusername, ghpassword)
    except Exception as details:
        print "Error - Connect to github failed: %s" % details


def get_bugsfromfix(parse, bzla, gh):
    out = []
    for ghuser in parse.get("github", "users").split():
        for ghname in cfgparse.get(ghuser, "repos").split():
            print "INFO - go through reop %s/%s" % (ghuser, ghname)
            bugs = get_ghlink2bugs(bzla, gh, ghuser, ghname)
            out.extend(bugs)
    return out


def get_bugsfromlist(list_file, bzla):
    bugs = []
    bug_list = map(lambda x: x.strip(), open(list_file).readlines())
    bug_list = [bug for bug in bug_list if bug]
    for bug_id in bug_list:
        try:
            bug = bzla.getbug(bug_id)
        except Exception:
            print "Error - bug: %s - %s" % (bug_id, details)
            continue
        bugs.append(bug)
    return bugs


if __name__ == "__main__":
    ini_cfg = "%s.ini" % os.path.splitext(sys.argv[0])[0]
    parser = argparse.ArgumentParser(
        description='Auto update bug status by fix')
    parser.add_argument('--bug-list', action='store',
                        dest='bug_list', help="Bug ID list file")
    parser.add_argument('--config-file', action='store',
                        dest='cfg_file', default=ini_cfg,
                        help="include github and bugzilla configuration")
    parser.add_argument('--version', action='version', version='%(prog)s 1.0')
    arguments = parser.parse_args()

    if not os.path.exists(arguments.cfg_file):
        print "Error - %s not exists" % arguments.cfg_file
        sys.exit(1)

    cfgparse = SafeConfigParser()
    cfgparse.read(arguments.cfg_file)

    gh = get_github(cfgparse)
    if not gh:
        sys.exit(2)

    bzla = get_bugzilla(cfgparse)
    if not bzla:
        sys.exit(3)

    bugs = get_bugsfromfix(cfgparse, bzla, gh)
    if arguments.bug_list:
        bugs += get_bugsfromlist(arguments.bug_list, bzla)
    bugs = [bug for bug in bugs if is_ready4qa(bug, gh)]
    map(move_state2onqa, bugs)
