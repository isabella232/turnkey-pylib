# 
# Copyright (c) 2007-2013 Liraz Siri <liraz@turnkeylinux.org>
# 
# This file is part of turnkey-pylib.
# 
# turnkey-pylib is open source software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 3 of
# the License, or (at your option) any later version.
# 
"""
Module that contains classes for capturing stdout/stderr.

Warning: if you aren't careful, exceptions raised after trapping stdout/stderr
will cause your program to exit silently.

StdTrap usage:
    trap = StdTrap()
    try:
        expression
    finally:
        trap.close()

    trapped_stdout = trap.stdout.read()
    trapped_stderr = trap.stderr.read()

UnitedStdTrap usage:

    trap = UnitedStdTrap()
    try:
        expression
    finally:
        trap.close()

    trapped_output = trap.std.read()

"""

import os
import sys
import pty
import select
from StringIO import StringIO

import signal

class Error(Exception):
    pass

class SignalEvent:
    SIG = signal.SIGUSR1
    
    @classmethod
    def send(cls, pid):
        """send signal event to pid"""
        os.kill(pid, cls.SIG)

    def _sighandler(self, sig, frame):
        self.value = True

    def __init__(self):
        self.value = False
        signal.signal(self.SIG, self._sighandler)
        
    def isSet(self):
        return self.value

    def clear(self):
        self.value = False
        
class Pipe:
    def __init__(self):
        r, w = os.pipe()
        self.r = os.fdopen(r, "r", 0)
        self.w = os.fdopen(w, "w", 0)

def set_blocking(fd, block):
    import fcntl
    arg = os.O_NONBLOCK
    if block:
        arg =~ arg
    fcntl.fcntl(fd, fcntl.F_SETFL, arg)

class Sink:
    def __init__(self, fd):
        if hasattr(fd, 'fileno'):
            fd = fd.fileno()

        self.fd = fd
        self.data = ''

    def buffer(self, data):
        self.data += data

    def write(self):
        try:
            written = os.write(self.fd, self.data)
        except:
            return False

        self.data = self.data[written:]
        if not self.data:
            return True
        return False

class StdTrap:
    class Splicer:
        """Inside the _splice method, stdout is intercepted at
        the file descriptor level by redirecting it to a pipe. Now
        whenever someone writes to stdout, we can read it out the
        other end of the pipe.

        The problem is that if we don't suck data out of this pipe then
        eventually if enough data is written to it the process writing to
        stdout will be blocked by the kernel, which means we'll be limited to
        capturing up to 65K of output and after that anything else will hang.
        So to solve that we create a splicer subprocess to get around the OS's
        65K buffering limitation. The splicer subprocess's job is to suck the
        pipe into a local buffer and spit it back out to:
        
        1) the parent process through a second pipe created for this purpose.
        2) If `transparent` is True then the data from the local pipe is
           redirected back to the original filedescriptor. 

        3) If `files` are provided then data from the local pipe is written into those files.
        """
        @staticmethod
        def _splice(spliced_fd, usepty, transparent, files=[]):
            """splice into spliced_fd -> (splicer_pid, splicer_reader, orig_fd_dup)"""
               
            # duplicate the fd we want to trap for safe keeping
            orig_fd_dup = os.dup(spliced_fd)

            # create a bi-directional pipe/pty
            # data written to w can be read from r
            if usepty:
                r, w = os.openpty()
            else:
                r, w = os.pipe()

            # splice into spliced_fd by overwriting it
            # with the newly created `w` which we can read from with `r`
            os.dup2(w, spliced_fd)
            os.close(w)
            
            outpipe = Pipe()

            # the child process uses this to signal the parent to continue
            # the parent uses this to signal the child to close
            signal_event = SignalEvent()
            
            splicer_pid = os.fork()
            if splicer_pid:
                signal_continue = signal_event
                
                outpipe.w.close()
                os.close(r)

                while not signal_continue.isSet():
                    pass

                return splicer_pid, outpipe.r, orig_fd_dup

            signal_closed = signal_event
            
            # child splicer
            outpipe.r.close()

            outpipe = outpipe.w

            # we don't need this copy of spliced_fd
            # keeping it open will prevent it from closing
            os.close(spliced_fd)

            set_blocking(r, False)
            set_blocking(outpipe.fileno(), False)
            
            poll = select.poll()
            poll.register(r, select.POLLIN | select.POLLHUP)
            
            closed = False
            SignalEvent.send(os.getppid())
            
            r_fh = os.fdopen(r, "r", 0)

            sinks = [ Sink(outpipe.fileno()) ]
            if files:
                sinks += [ Sink(f) for f in files ]
            if transparent:
                sinks.append(Sink(orig_fd_dup))

            while True:
                has_unwritten_data = True in [ sink.data != '' for sink in sinks ]

                if not closed:
                    closed = signal_closed.isSet()

                if closed and not has_unwritten_data:
                    break

                try:
                    events = poll.poll(1)
                except select.error:
                    events = ()

                for fd, mask in events:
                    if fd == r:
                        if mask & select.POLLIN:

                            data = r_fh.read()
                            for sink in sinks:
                                sink.buffer(data)
                                poll.register(sink.fd)

                            poll.register(outpipe.fileno(), select.POLLOUT)

                        if mask & select.POLLHUP:
                            closed = True
                            poll.unregister(fd)
                            
                    else:
                        for sink in sinks:
                            if sink.fd != fd:
                                continue

                            if mask & select.POLLOUT:
                                wrote_all = sink.write()
                                if wrote_all:
                                    poll.unregister(sink.fd)

            os._exit(0)
      
        def __init__(self, spliced_fd, usepty=False, transparent=False, files=[]):
            vals = self._splice(spliced_fd, usepty, transparent, files)
            self.splicer_pid, self.splicer_reader, self.orig_fd_dup = vals

            self.spliced_fd = spliced_fd

        def close(self):
            """closes the splice -> captured output"""
            # dupping orig_fd_dup -> spliced_fd does two things:
            # 1) it closes spliced_fd - signals our splicer process to stop reading
            # 2) it overwrites spliced_fd with a dup of the unspliced original fd
            os.dup2(self.orig_fd_dup, self.spliced_fd)
            SignalEvent.send(self.splicer_pid)
            
            os.close(self.orig_fd_dup)

            captured = self.splicer_reader.read()
            os.waitpid(self.splicer_pid, 0)

            return captured

    def __init__(self, stdout=True, stderr=True, usepty=False, transparent=False):

        self.usepty = pty
        self.transparent = transparent

        self.stdout_splice = None
        self.stderr_splice = None
        
        if stdout:
            sys.stdout.flush()
            self.stdout_splice = StdTrap.Splicer(sys.stdout.fileno(), usepty, transparent)

        if stderr:
            sys.stderr.flush()
            self.stderr_splice = StdTrap.Splicer(sys.stderr.fileno(), usepty, transparent)
            
        self.stdout = None
        self.stderr = None

    def close(self):
        if self.stdout_splice:
            sys.stdout.flush()
            self.stdout = StringIO(self.stdout_splice.close())

        if self.stderr_splice:
            sys.stderr.flush()
            self.stderr = StringIO(self.stderr_splice.close())

class UnitedStdTrap(StdTrap):
    def __init__(self, usepty=False, transparent=False, files=[]):
        self.usepty = usepty
        self.transparent = transparent
        
        sys.stdout.flush()
        self.stdout_splice = self.Splicer(sys.stdout.fileno(), usepty, transparent, files)

        sys.stderr.flush()
        self.stderr_dupfd = os.dup(sys.stderr.fileno())
        os.dup2(sys.stdout.fileno(), sys.stderr.fileno())

        self.std = self.stderr = self.stdout = None

    def close(self):
        sys.stdout.flush()
        self.std = self.stderr = self.stdout = StringIO(self.stdout_splice.close())

        sys.stderr.flush()
        os.dup2(self.stderr_dupfd, sys.stderr.fileno())
        os.close(self.stderr_dupfd)

def silence(callback, args=()):
    """convenience function - traps stdout and stderr for callback.
    Returns (ret, trapped_output)
    """
    
    trap = UnitedStdTrap()
    try:
        ret = callback(*args)
    finally:
        trap.close()

    return ret

def getoutput(callback, args=()):
    trap = UnitedStdTrap()
    try:
        callback(*args)
    finally:
        trap.close()

    return trap.std.read()

def test(transparent=False):
    def sysprint():
        os.system("echo echo stdout")
        os.system("echo echo stderr 1>&2")

    print "--- 1:"
    
    s = UnitedStdTrap(transparent=transparent)
    print "printing to united stdout..."
    print >> sys.stderr, "printing to united stderr..."
    sysprint()
    s.close()

    print 'trapped united stdout and stderr: """%s"""' % s.std.read()
    print >> sys.stderr, "printing to stderr"

    print "--- 2:"
    
    s = StdTrap(transparent=transparent)
    s.close()
    print 'nothing in stdout: """%s"""' % s.stdout.read()
    print 'nothing in stderr: """%s"""' % s.stderr.read()

    print "--- 3:"

    s = StdTrap(transparent=transparent)
    print "printing to stdout..."
    print >> sys.stderr, "printing to stderr..."
    sysprint()
    s.close()

    print 'trapped stdout: """%s"""' % s.stdout.read()
    print >> sys.stderr, 'trapped stderr: """%s"""' % s.stderr.read()


def test2():
    trap = StdTrap(stdout=True, stderr=True, transparent=False)

    try:
        for i in range(1000):
            print "A" * 70
            sys.stdout.flush()
            print >> sys.stderr, "B" * 70
            sys.stderr.flush()
            
    finally:
        trap.close()

    assert len(trap.stdout.read()) == 71000
    assert len(trap.stderr.read()) == 71000

def test3():
    trap = UnitedStdTrap(transparent=True)
    try:
        for i in range(10):
            print "A" * 70
            sys.stdout.flush()
            print >> sys.stderr, "B" * 70
            sys.stderr.flush()
    finally:
        trap.close()

    print len(trap.stdout.read())

def test4():
    import time
    s = StdTrap(transparent=True)
    s.close()
    print 'nothing in stdout: """%s"""' % s.stdout.read()
    print 'nothing in stderr: """%s"""' % s.stderr.read()

if __name__ == '__main__':
    test(False)
    print
    print "=== TRANSPARENT MODE ==="
    print
    test(True)
    test2()
    test3()
