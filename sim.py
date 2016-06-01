import sys, random, socket
import time, traceback, argparse
from xvfbwrapper import Xvfb
import multiprocessing as mp
import Queue
import requests

# disable warnings when posting to our tsdb
# with self-signed cert
from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

from workflows import *
from user import *

HOUR_TO_SEC = 3600
# frequency to check on users, spin up new users, etc
USER_MONITOR_INTERVAL = 0.5
# frequency to being push to tsdb. note this is
# the minimum value; it will be used after a push
# has completed
TSDB_PUSH_INTERVAL = 1

def parse_args():
    parser = argparse.ArgumentParser(description="Spin up some simulated users")
    parser.add_argument("-u", "--url",
            help="full url of zenoss UI")
    parser.add_argument("-n", "--username",
            help="zenoss account username")
    parser.add_argument("-p", "--password",
            help="zenoss account password")
    parser.add_argument("--chromedriver-path", dest="chromedriver",
            default=None, help="path to chromedriver")
    parser.add_argument("-c", "--users",
            default=1, help="number of simulated users to create", type=int)
    parser.add_argument("--log-dir", dest="logDir",
            default="/tmp/", help="directory to store logs")
    parser.add_argument("--headless",
            dest="headless", action="store_true", help="if simulations should run headless")
    parser.add_argument("--no-headless",
            dest="headless", action="store_false", help="if simulations should run headless")
    parser.add_argument('--duration', dest = 'duration', default = 0,
            help = 'duration in seconds that workflows will be repeated', type = float)
    parser.add_argument('--workflows', dest = 'workflows', default = '',
            help = 'workflows to run, a comma separated string')
    parser.add_argument('--tsdb-url', dest = 'tsdbUrl', default = '',
            help = 'OpenTSDB URL')
    parser.add_argument('--leader', dest = 'leader', action = 'store_true',
            help = 'the leader processes additional stats')

    parser.set_defaults(headless=True, leader=False)

    # TODO - skill level
    # TODO - more sensible defaults and argument config

    args = parser.parse_args()

    # TODO - pretty sure argparse can do this by default
    # and also better
    if not args.url:
        raise Exception("url is required")
    if not args.username:
        raise Exception("username is required")
    if not args.password:
        raise Exception("password is required")
    if not args.workflows:
        raise Exception('workflows are required')
    else:
        return args

def startUser(name, url, username, password, headless, logDir, chromedriver,
        duration, workflowNames, tsdbQueue):
    if headless:
        xvfb = Xvfb(width=1100, height=800)
        xvfb.start()

    user = User(name, url=url, username=username, password=password,
            logDir=logDir, chromedriver=chromedriver, duration=duration,
            tsdbQueue=tsdbQueue)

    # Always start with Login() and end with Logout(). There has to be at least
    # one workflow between Login() and Logout().
    workflows = []
    workflows.append(LogInOutWorkflow.Login())

    workflowNames = [name.strip() for name in workflowNames.split(',')]
    for name in workflowNames:
        try:
            workflowClass = getattr(sys.modules[__name__], name)
        except AttributeError:
            user.log('Could not find workflow {}'.format(name), severity="ERROR")
            user.quit()

        workflows.append(workflowClass())

    workflows.append(LogInOutWorkflow.Logout())

    user.addWorkflow(workflows)

    try:
        user.work()
    except KeyboardInterrupt:
        pass
    except Exception:
        user.log("%s raised an uncaught exception" % user.name, severity="ERROR")
        traceback.print_exc()
    finally:
        print "cleaning up %s" % user.name
        user.quit()
        if headless:
            xvfb.stop()
        print "cleaned up %s" % user.name

def pushToTsdb(url, queue):
    data = []
    obj = None
    try:
        while queue.qsize() and len(data) < 15:
            obj = queue.get()
            data += obj
    except Queue.Empty:
        pass

    headers={'Content-type': 'application/json', 'Accept': 'text/plain'}
    if data:
        r = requests.post(
                url + "/api/put",
                data=json.dumps(data), headers=headers, verify=False)
        if r.status_code != 204:
            print "Failed to post %i datapoints to tsdb: %i" % (len(data), r.status_code)
        else:
            print "Posted %i datapoints to tsdb, %i left in queue" % (len(data), queue.qsize())

def countUser(tsdbUrl, startTime, endTime):
    headers={'Content-type': 'application/json', 'Accept': 'text/plain'}
    url = (tsdbUrl + "/api/query"
           + '?start=' + time.strftime('%Y/%m/%d-%H:%M:%S', time.gmtime(startTime))
           + '&end=' + time.strftime('%Y/%m/%d-%H:%M:%S', time.gmtime(endTime))
           + '&m=zimsum:userCountDelta')
    r = requests.get(
            url, data=json.dumps({}), headers=headers, verify=False)

    userCountDelta = r.json()[0]['dps']
    sortedTime = sorted(userCountDelta.keys())
    count = 0
    data = []
    for t in sortedTime:
        count += userCountDelta[t]
        data += [{'timestamp': float(t), 'metric': 'userCount', 'value': count, 'tags': {'host': socket.gethostname()}}]

    r = requests.post(
            tsdbUrl + "/api/put",
            data=json.dumps(data), headers=headers, verify=False)
    if not r.ok:
        print 'Failed to post user count to tsdb'
        print r['error']['message']

names = ["Obak", "Uglug", "Oldog", "Olfil", "Shagrat", "Mauhagr",
        "Oldolg", "Othrol", "Lagdush", "Orgod"]

if __name__ == '__main__':
    try:
        args = parse_args()

        # create a log directory for this run
        args.logDir = "%s/%s" % (args.logDir, time.time())
        if not os.path.exists(args.logDir):
            os.makedirs(args.logDir)

        # TODO - log additional args
        print ("starting %i users with options:\n"
            "    url: %s\n"
            "    username: %s\n"
            "    headless: %s\n"
            "    workflows: %s\n"
            "    duration: %is\n"
            "    logDir: %s") % (
                args.users, args.url, args.username, "True" if args.headless else "False", args.workflows, args.duration, args.logDir)

        tsdbQueue = mp.Queue()

        startTime = time.time()
        processes = []
        done = 0
        userCount = 0
        shouldWork = True
        lastPush = time.time()

        # if its worktime or there are any processes
        # left working, do work!
        while len(processes) or shouldWork:
            # check for users that have stopped
            # TODO - distinguish failed from completed
            toRemove = []
            for p in processes:
                if not p.is_alive():
                    toRemove.append(p)
                    done += 1
                    print colorizeString("%i users done so far, %i currently running" % (done, len(processes)), "DEBUG")

            # Push the number of quitters to tsdb
            if toRemove:
                key = 'userCountDelta'
                value = -len(toRemove)
                tags = {'host': socket.gethostname()}
                data = [{'timestamp': time.time(), 'metric': key, 'value': value, 'tags': tags}]
                tsdbQueue.put(data)
                print 'Pushed the number of quitters {} to tsdb'.format(value)

            # remove any dead users
            for p in toRemove:
                processes.remove(p)

            remainingWorkTime = args.duration - (time.time() - startTime)

            # if work should continue, add users to keep process list full
            userIncrement = 0
            if shouldWork and len(processes) < args.users:
                # TODO - skill level
                userName = "%s_%i" % (random.choice(names), userCount)
                p = mp.Process(target=startUser, args=(
                    userName, args.url, args.username, args.password,
                    args.headless, args.logDir, args.chromedriver,
                    remainingWorkTime, args.workflows, tsdbQueue))
                userCount += 1
                userIncrement += 1
                print colorizeString("%s - started user %s" % (time.asctime(), userName), "DEBUG")
                processes.append(p)
                p.start()
                # give xvfb time to grab a display before kicking off
                # a new request
                # prevent all users from logging in at once
                time.sleep(4)

            # Push the number of quitters to tsdb
            if userIncrement:
                key = 'userCountDelta'
                value = userIncrement
                tags = {'host': socket.gethostname()}
                data = [{'timestamp': time.time(), 'metric': key, 'value': value, 'tags': tags}]
                tsdbQueue.put(data)
                print 'Pushed the number of new users {} to tsdb'.format(value)

            # is it quittin time yet?
            if remainingWorkTime < 0 and shouldWork:
                shouldWork = False
                print colorizeString("%s - it's quitting time yall. finish what youre doing" % time.asctime(), "DEBUG")

            # push some stats?
            now = time.time()
            if now - lastPush > TSDB_PUSH_INTERVAL:
                pushToTsdb(args.tsdbUrl, tsdbQueue)
                lastPush = time.time()

            time.sleep(USER_MONITOR_INTERVAL)
    except:
        traceback.print_exc()

    finally:
        # drain stats queue
        while tsdbQueue.qsize():
            print "draining queue with size %i" % tsdbQueue.qsize()
            pushToTsdb(args.tsdbUrl, tsdbQueue)
        tsdbQueue.close()
        tsdbQueue.join_thread()
        endTime = time.time()
        print colorizeString("all processes have exited", "DEBUG")
        print colorizeString("start: %s, end: %s" % \
                (time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(startTime)),
                    time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(endTime))), "DEBUG")
        if args.leader:
            countUser(args.tsdbUrl, startTime, endTime)
