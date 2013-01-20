# Package:  manager
# Date:     11th April 2010
# Author:   James Mills, prologic at shortcircuit dot net dot au
import thread

"""
This module defines the Manager class.
"""

import atexit
from itertools import chain
from collections import deque
from inspect import isfunction
from uuid import uuid4 as uuid
from traceback import format_tb
from sys import exc_info as _exc_info
from weakref import WeakValueDictionary
from signal import signal, SIGINT, SIGTERM
from types import MethodType, GeneratorType
from threading import current_thread, Thread, RLock
from multiprocessing import current_process, Process

from .values import Value
from .handlers import handler
from .events import Done, Success, Failure, Complete
from .events import Error, Started, Stopped, Signal, GenerateEvents

TIMEOUT = 0.1  # 100ms timeout when idle


def _sortkey(handler):
    return (handler.priority, handler.filter)


class CallValue(object):
    def __init__(self, value):
        self.value = value


class Manager(object):
    """
    The manager class has two roles. As a base class for component
    implementation, it provides methods for event and handler management.
    The method :meth:`.fireEvent` appends a new event at the end of the event
    queue for later execution. :meth:`.waitEvent` suspends the execution
    of a handler until all handlers for a given event have been invoked.
    :meth:`.callEvent` combines the last two methods in a single method.

    The methods :meth:`.addHandler` and :meth:`.removeHandler` allow handlers
    for events to be added and removed dynamically. (The more common way to
    register a handler is to use the :func:`~.handlers.handler` decorator
    or derive the class from :class:`~.components.Component`.)

    In its second role, the :class:`.Manager` takes the role of the
    event executor. Every component hierarchy has a root component that
    maintains a queue of events. Firing an event effectively means
    appending it to the event queue maintained by the root manager.
    The :meth:`.flush` method removes all pending events from the
    queue and, for each event, invokes all the handlers. Usually,
    :meth:`.flush` is indirectly invoked by :meth:`run`.

    The manager optionally provides information about the execution of
    events as automatically generated events. If an :class:`~.events.Event`
    has its :attr:`success` attribute set to True, the manager fires
    a :class:`~.events.Success` event if all handlers have been
    executed without error. Note that this event will be
    enqueued (and dispatched) immediately after the events that have been
    fired by the event's handlers. So the success event indicates both
    the successful invocation of all handlers for the event and the
    processing of the immediate follow-up events fired by those handlers.

    Sometimes it is not sufficient to know that an event and its
    immediate follow-up events have been processed. Rather, it is
    important to know when all state changes triggered by an event,
    directly or indirectly, have been performed. This also includes
    the processing of events that have been fired when invoking
    the handlers for the follow-up events and the processing of events
    that have again been fired by those handlers and so on. The completion
    of the processing of an event and all its direct or indirect
    follow-up events may be indicated by a :class:`~.events.Complete`
    event. This event is generated by the manager if :class:`~.events.Event`
    has its :attr:`complete` attribute set to True.

    Apart from the event queue, the root manager also maintains  a list of
    tasks, actually Python generators, that are updated when the event queue
    has been flushed.
    """

    _currently_handling = None
    traverse_children_handlers = True
    """
    The event currently being handled.
    """

    def __init__(self, *args, **kwargs):
        "initializes x; see x.__class__.__doc__ for signature"

        self._tasks = set()
        self._cache = dict()
        self._cache_needs_refresh = False
        self._queue = deque()
        self._flush_batch = 0
        self._globals = set()
        self._handlers = dict()
        self._values = WeakValueDictionary()

        self._executing_thread = None
        self._running = False
        self._thread = None
        self._process = None
        self._lock = RLock()

        self.root = self.parent = self
        self.components = set()

    def __repr__(self):
        "x.__repr__() <==> repr(x)"

        name = self.__class__.__name__

        channel = "/{0:s}".format(getattr(self, "channel", ""))

        q = len(self._queue)
        state = "R" if self.running else "S"

        pid = current_process().pid

        if pid:
            id = "%s:%s" % (pid, current_thread().getName())
        else:
            id = current_thread().getName()

        format = "<%s%s %s (queued=%d) [%s]>"
        return format % (name, channel, id, q, state)

    def __contains__(self, y):
        """x.__contains__(y) <==> y in x

        Return True if the Component y is registered.
        """

        components = self.components.copy()
        return y in components or y in [c.__class__ for c in components]

    def __len__(self):
        """x.__len__() <==> len(x)

        Returns the number of events in the Event Queue.
        """

        return len(self._queue)

    def __add__(self, y):
        """x.__add__(y) <==> x+y

        (Optional) Convenience operator to register y with x
        Equivalent to: y.register(x)

        @return: x
        @rtype Component or Manager
        """

        y.register(self)
        return self

    def __iadd__(self, y):
        """x.__iadd__(y) <==> x += y

        (Optional) Convenience operator to register y with x
        Equivalent to: y.register(x)

        @return: x
        @rtype Component or Manager
        """

        y.register(self)
        return self

    def __sub__(self, y):
        """x.__sub__(y) <==> x-y

        (Optional) Convenience operator to unregister y from x.manager
        Equivalent to: y.unregister()

        @return: x
        @rtype Component or Manager
        """

        if y.manager is not y:
            y.unregister()
        return self

    def __isub__(self, y):
        """x.__sub__(y) <==> x -= y

        (Optional) Convenience operator to unregister y from x
        Equivalent to: y.unregister()

        @return: x
        @rtype Component or Manager
        """

        if y.manager is not y:
            y.unregister()
        return self

    @property
    def name(self):
        """Return the name of this Component/Manager"""

        return self.__class__.__name__

    @property
    def running(self):
        """Return the running state of this Component/Manager"""

        return self._running

    def getHandlers(self, event, channel, **kwargs):
        channel_is_instance = isinstance(channel, Manager)

        name = event.name
        handlers = set()

        handlers_chain = [self._handlers.get("*", set())]

        if not channel_is_instance or channel == self:
            if name in self._handlers:
                handlers_chain.append(self._handlers[name])

        for _handler in chain(*handlers_chain):
            if _handler.channel:
                handler_channel = _handler.channel
            elif hasattr(_handler, "__self__"):
                handler_channel = getattr(_handler.__self__, "channel", None)
            else:
                handler_channel = None

            if channel == "*" or handler_channel in ("*", channel,) \
                    or channel_is_instance:
                handlers.add(_handler)

        if not kwargs.get("exclude_globals", False):
            handlers.update(self._globals)

        if self.traverse_children_handlers:
            for c in self.components.copy():
                handlers.update(c.getHandlers(event, channel, **kwargs))

        return handlers

    def addHandler(self, f):
        method = MethodType(f, self, self.__class__) if isfunction(f) else f

        setattr(self, method.__name__, method)

        if not method.names and method.channel == "*":
            self._globals.add(method)
        elif not method.names:
            self._handlers.setdefault("*", set()).add(method)
        else:
            for name in method.names:
                self._handlers.setdefault(name, set()).add(method)

        self.root._cache_needs_refresh = True

        return method

    def removeHandler(self, method, event=None):
        if event is None:
            names = method.names
        else:
            names = [event]

        for name in names:
            self._handlers[name].remove(method)
            if not self._handlers[name]:
                del self._handlers[name]
                try:
                    delattr(self, method.__name__)
                except AttributeError:
                    # Handler was never part of self
                    pass

        self.root._cache_needs_refresh = True

    def registerChild(self, component):
        self.components.add(component)
        self.root._queue.extend(list(component._queue))
        component._queue.clear()
        self.root._cache_needs_refresh = True

    def unregisterChild(self, component):
        self.components.remove(component)
        self.root._cache_needs_refresh = True

    def _fire(self, event, channel):
        # check if event is fired while handling an event
        if thread.get_ident() == self._executing_thread \
                and not isinstance(event, Signal):
            if self._currently_handling is not None \
                    and getattr(self._currently_handling, "cause", None):
                # if the currently handled event wants to track the
                # events generated by it, do the tracking now
                event.cause = self._currently_handling
                event.effects = 1
                self._currently_handling.effects += 1

            self._queue.append((event, channel))

        # the event comes from another thread
        else:
            # Another thread has provided us with something to do.
            # If the component is running, we must make sure that
            # any pending generate event waits no longer, as there
            # is something to do now.
            with self._lock:
                # We don't lock around self._currently_handling = None,
                # so it made change after checking 
                handling = self._currently_handling
                if isinstance(handling, GenerateEvents):
                    self._queue.append((event, channel))
                    handling.reduce_time_left(0)
                else:
                    self._queue.append((event, channel))

    def fireEvent(self, event, *channels):
        """Fire an event into the system.

        :param event: The event that is to be fired.
        :param channels: The channels that this event is delivered on.
           If no channels are specified, the event is delivered to the
           channels found in the event's :attr:`channel` attribute.
           If this attribute is not set, the event is delivered to
           the firing component's channel. And eventually,
           when set neither, the event is delivered on all
           channels ("*").
        """

        if not channels:
            channels = event.channels \
                or (getattr(self, "channel", "*"),) \
                or ("*",)

        event.channels = channels

        event.value = Value(event, self)
        self.root._fire(event, channels)

        return event.value

    fire = fireEvent

    def registerTask(self, g):
        self._tasks.add(g)

    def unregisterTask(self, g):
        if g in self._tasks:
            self._tasks.remove(g)

    def waitEvent(self, event, *channels):
        state = {
            'run': False,
            'flag': False,
            'event': None,
        }
        _event = event

        def _on_event(self, event, *args, **kwargs):
            if not state['run']:
                self.removeHandler(_on_event_handler, _event)
                event.alert_done = True
                state['run'] = True
                state['event'] = event

        def _on_done(self, event, source, *args, **kwargs):
            if state['event'] == source:
                state['flag'] = True

        if not channels:
            channels = (None, )

        for channel in channels:
            _on_event_handler = self.addHandler(
                handler(event, channel=channel)(_on_event))
            _on_done_handler = self.addHandler(
                handler("%s_done" % event, channel=channel)(_on_done))

        while not state['flag']:
            yield None

        self.removeHandler(_on_done_handler, "%s_done" % event)

    wait = waitEvent

    def callEvent(self, event, *channels):
        """
        Fire the given event to the specified channels and suspend
        execution until it has been dispatched. This method may only
        be invoked as argument to a ``yield`` on the top execution level
        of a handler (e.g. "``yield self.callEvent(event)``").
        It effectively creates and returns a generator
        that will be invoked by the main loop until the event has
        been dispatched (see :func:`circuits.core.handlers.handler`).
        """
        value = self.fire(event, *channels)
        for r in self.waitEvent(event.name, event.channels):
            yield r
        yield CallValue(value)

    call = callEvent

    def _flush(self):
        # if _flush is not called from tick, set executing thread
        set_executing = (self._executing_thread is None)
        if set_executing:
            self._executing_thread = thread.get_ident()

        # Handle events currently on queue, but none of the newly generated
        # events. Note that _flush can be called recursively (e.g. when 
        # handling a Stop event).
        if self._flush_batch == 0:
            self._flush_batch = len(self._queue)
        while self._flush_batch > 0:
            self._flush_batch -= 1 # Decrement first!
            event, channels = self._queue.popleft()
            self._dispatcher(event, channels, self._flush_batch)

        # restore executing thread if necessary
        if set_executing:
            self._executing_thread = None

    def flushEvents(self):
        """
        Flush all Events in the Event Queue. If called on a manager
        that is not the root of an object hierarchy, the invocation
        is delegated to the root manager.
        """

        self.root._flush()

    flush = flushEvents

    def _dispatcher(self, event, channels, remaining):
        if event.complete:
            if not getattr(event, "cause", None):
                event.cause = event
            event.effects = 1  # event itself counts (must be done)
        eargs = event.args
        ekwargs = event.kwargs

        if self._cache_needs_refresh:
            # Don't call self._cache.clear() from other threads,
            # this may interfere with cache rebuild.
            self._cache.clear()
        if (event.name, channels) in self._cache:
            handlers = self._cache[(event.name, channels)]
        else:
            h = (self.getHandlers(event, channel) for channel in channels)
            handlers = sorted(chain(*h), key=_sortkey, reverse=True)
            if isinstance(event, GenerateEvents):
                from .helpers import FallBackGenerator
                handlers.append(FallBackGenerator()._on_generate_events)
            self._cache[(event.name, channels)] = handlers

        if isinstance(event, GenerateEvents):
            with self._lock:
                self._currently_handling = event
                if self or remaining > 0 or not self._running:
                    event.reduce_time_left(0)
                elif len(self._tasks) > 0:
                    event.reduce_time_left(TIMEOUT) 
                # From now on, firing an event will reduce time left
                # to 0, which prevents handlers from waiting (or wakes
                # them up with resume if they should be waiting already)
        else:
            self._currently_handling = event

        value = None
        error = None

        for handler in handlers:
            event.handler = handler
            try:
                if handler.event:
                    value = handler(event, *eargs, **ekwargs)
                else:
                    value = handler(*eargs, **ekwargs)
            except (KeyboardInterrupt, SystemExit):
                self.stop()
            except:
                etype, evalue, etraceback = _exc_info()
                traceback = format_tb(etraceback)
                error = (etype, evalue, traceback)

                event.value.errors = True

                value = error

                if event.failure:
                    self.fire(
                        Failure.create("Failure", event, error),
                        *event.channels
                    )

                self.fire(Error(etype, evalue, traceback, handler))

            if isinstance(value, GeneratorType):
                event.waitingHandlers += 1
                event.value.promise = True
                self.registerTask((event, value))
            elif value is not None:
                event.value.value = value

            if value and handler.filter:
                break

        self._currently_handling = None
        self._eventDone(event, error)

    def _eventDone(self, event, error=None):
        if event.waitingHandlers:
            return

        # The "%s_Done" event is for internal use by waitEvent only.
        # Use the "%s_Success" event in you application if you are
        # interested in being notified about the last handler for
        # an event having been invoked.
        if event.alert_done:
            self.fire(
                Done.create("Done", event, event.value.value),
                *event.channels
            )

        if error is None and event.success:
            channels = getattr(event, "success_channels", event.channels)
            self.fire(
                Success.create("Success", event, event.value.value),
                *channels
            )

        while True:
            # cause attributes indicates interest in completion event
            cause = getattr(event, "cause", None)
            if not cause:
                break
            # event takes part in complete detection (as nested or root event)
            event.effects -= 1
            if event.effects > 0:
                break  # some nested events remain to be completed
            if event.complete:  # does this event want signaling?
                self.fire(
                    Complete.create("Complete", event, event.value.value),
                    *getattr(event, "complete_channels", event.channels)
                )
            # this event and nested events are done now
            delattr(event, "cause")
            delattr(event, "effects")
            # cause has one of its nested events done, decrement and check
            event = cause

    def _signalHandler(self, signal, stack):
        self.fire(Signal(signal, stack))
        if signal in [SIGINT, SIGTERM]:
            self.stop()

    def start(self, process=False, link=None):
        """
        Start a new thread or process that invokes this manager's
        ``run()`` method. The invocation of this method returns
        immediately after the task or process has been started.
        """

        if process:
            # Parent<->Child Bridge
            if link is not None:
                from circuits.net.sockets import Pipe
                from circuits.core.bridge import Bridge

                channels = (uuid(),) * 2
                parent, child = Pipe(*channels)
                Bridge(parent, channel=channels[0]).register(link)

                args = (child,)
            else:
                args = ()

            self._process = Process(target=self.run, args=args, name=self.name)
            self._process.daemon = True
            self._process.start()
        else:
            self._thread = Thread(target=self.run, name=self.name)
            self._thread.daemon = True
            self._thread.start()

    def join(self):
        if getattr(self, "_thread", None) is not None:
            return self._thread.join()

        if getattr(self, "_process", None) is not None:
            return self._process.join()

    def stop(self):
        """
        Stop this manager. Invoking this method either causes
        an invocation of ``run()`` to return or terminates the
        thread or process associated with the manager.
        """
        if not self.running:
            return

        self._running = False

        self.fire(Stopped(self))
        for _ in range(3):
            self.tick()

        self._thread = None
        self._process = None

    def processTask(self, event, task, parent=None):
        value = None
        try:
            value = task.next()
            if isinstance(value, CallValue):
                # Done here, next() will StopIteration anyway
                self.unregisterTask((event, task, parent))
                # We are in a callEvent
                value = parent.send(value.value)
                if isinstance(value, GeneratorType):
                    # We loose a yield but we gain one, we don't need to change
                    # event.waitingHandlers
                    self.registerTask((event, value, parent))
                    self.processTask(event, value, parent)
                else:
                    event.waitingHandlers -= 1
                    if value is not None:
                        event.value.value = value
                    self.registerTask((event, parent))
            elif isinstance(value, GeneratorType):
                event.waitingHandlers += 1
                self.registerTask((event, value, task))
                self.unregisterTask((event, task))
                # We want to process all the tasks because
                # we bind handlers in there
                self.processTask(event, value, task)
            elif value is not None:
                event.value.value = value
        except StopIteration:
            event.waitingHandlers -= 1
            if parent:
                self.unregisterTask((event, task, parent))
            else:
                self.unregisterTask((event, task))
            if parent:
                self.registerTask((event, parent))
            elif event.waitingHandlers == 0:
                event.value.inform(True)
                self._eventDone(event)
        except (KeyboardInterrupt, SystemExit):
            self.stop()
        except:
            self.unregisterTask((event, task))

            etype, evalue, etraceback = _exc_info()
            traceback = format_tb(etraceback)
            error = (etype, evalue, traceback)

            event.value.value = error
            event.value.errors = True
            event.value.inform(True)

            if event.failure:
                self.fire(
                    Failure.create("Failure", event, error),
                    *event.channels
                )

            self.fire(Error(etype, evalue, traceback, event.handler))

    def tick(self, timeout=-1):
        """
        Execute all possible actions once. Process all registered tasks
        and flush the event queue. If the application is running fire a
        GenerateEvents to get new events from sources.

        This method is usually invoked from :meth:`~.run`. It may also be
        used to build an application specific main loop.

        :param timeout: the maximum waiting time spent in this method. If
            negative, the method may block until at least one action
            has been taken.
        :type timeout: float, measuring seconds
        """
        self._executing_thread = thread.get_ident()

        # process tasks
        for task in self._tasks.copy():
            self.processTask(*task)

        if self._running:
            self.fire(GenerateEvents(self._lock, timeout), "*")

        if self:
            self.flush()

        self._executing_thread = None

    def run(self, socket=None):
        """
        Run this manager. The method fires the
        :class:`~.events.Started` event and then continuously
        calls :meth:`~.tick`.

        The method returns when the manager's :meth:`~.stop` method is invoked.

        If invoked by a programs main thread, a signal handler for
        the ``INT`` and ``TERM`` signals is installed. This handler
        fires the corresponding :class:`~.events.Signal`
        events and then calls :meth:`~.stop` for the manager.
        """

        atexit.register(self.stop)

        if current_thread().getName() == "MainThread":
            try:
                signal(SIGINT, self._signalHandler)
                signal(SIGTERM, self._signalHandler)
            except ValueError:
                # Ignore if we can't install signal handlers
                pass

        self._running = True
        self._executing_thread = current_thread()

        # Setup Communications Bridge

        if socket is not None:
            from circuits.core.bridge import Bridge
            Bridge(socket, channel=socket.channel).register(self)

        self.fire(Started(self))

        try:
            while self or self.running:
                self.tick()
        except:
            pass
        finally:
            try:
                self.tick()
            except:
                pass
