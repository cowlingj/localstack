import functools
import inspect
import threading
import typing as t
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    Iterable,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Protocol,
    Type,
    TypeVar,
    Union,
    overload,
)

from werkzeug import Request, Response
from werkzeug.routing import BaseConverter, Map, Rule, RuleFactory

from .request import get_raw_path

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE")

E = TypeVar("E")
RequestArguments = Mapping[str, Any]


class RegexConverter(BaseConverter):
    """
    A converter that can be used to inject a regex as parameter, e.g., ``path=/<regex('[a-z]+'):my_var>``.
    When using groups in regex, make sure they are non-capturing ``(?:[a-z]+)``
    """

    def __init__(self, map: "Map", *args: Any, **kwargs: Any) -> None:
        super().__init__(map, *args, **kwargs)
        self.regex = args[0]


class PortConverter(BaseConverter):
    """
    Useful to optionally match ports for host patterns, like ``localstack.localhost.cloud<port:port>``. Notice how you
    don't need to specify the colon. The regex matches it if the port is there, and will remove the colon if matched.
    The converter converts the port to an int, or returns None if there's no port in the input string.
    """

    regex = r"(?::[0-9]{1,5})?"

    def to_python(self, value: str) -> Any:
        if value:
            return int(value[1:])
        return None


class Dispatcher(Protocol[E]):
    """
    A Dispatcher is called when a URL route matches a request. The dispatcher is responsible for appropriately
    creating a Response from the incoming Request and the matching endpoint.
    """

    def __call__(self, request: Request, endpoint: E, args: RequestArguments) -> Response:
        """
        Dispatch the HTTP Request.

        :param request: the incoming HTTP request
        :param endpoint: the endpoint that matched the URL rule
        :param args: the request arguments extracted from the URL rule
        :return: an HTTP Response
        """
        pass


class _RuleAttributes(NamedTuple):
    path: str
    host: Optional[str] = (None,)
    methods: Optional[Iterable[str]] = None
    kwargs: Optional[Dict[str, Any]] = {}


class _RouteEndpoint(Protocol):
    """
    An endpoint that encapsulates ``_RuleAttributes`` for the creation of a ``Rule`` inside a ``Router``.
    """

    rule_attributes: list[_RuleAttributes]

    def __call__(self, *args, **kwargs):
        raise NotImplementedError


def route(
    path: str, host: Optional[str] = None, methods: Optional[Iterable[str]] = None, **kwargs
) -> Callable[[E], list[_RouteEndpoint]]:
    """
    Decorator that indicates that the given function is a Router Rule.

    :param path: the path pattern to match
    :param host: an optional host matching pattern. if not pattern is given, the rule matches any host
    :param methods: the allowed HTTP methods for this rule
    :param kwargs: any other argument that can be passed to ``werkzeug.routing.Rule``
    :return: the function endpoint wrapped as a ``_RouteEndpoint``
    """

    def wrapper(fn: E):
        if hasattr(fn, "rule_attributes"):
            route_marker = fn
        else:

            @functools.wraps(fn)
            def route_marker(*args, **kwargs):
                return fn(*args, **kwargs)

            route_marker.rule_attributes = []

        route_marker.rule_attributes.append(_RuleAttributes(path, host, methods, kwargs))

        return route_marker

    return wrapper


def call_endpoint(
    request: Request,
    endpoint: Callable[[Request, RequestArguments], Response],
    args: RequestArguments,
) -> Response:
    """
    A Dispatcher that treats the matching endpoint as a callable and invokes it with the Request and request arguments.
    """
    return endpoint(request, args)


def _clone_map_without_rules(old: Map) -> Map:
    return Map(
        default_subdomain=old.default_subdomain,
        strict_slashes=old.strict_slashes,
        merge_slashes=old.merge_slashes,
        redirect_defaults=old.redirect_defaults,
        converters=old.converters,
        sort_parameters=old.sort_parameters,
        sort_key=old.sort_key,
        host_matching=old.host_matching,
    )


class Router(Generic[E]):
    """
    A Router is a wrapper around werkzeug's routing Map, that adds convenience methods and additional dispatching
    logic via the ``Dispatcher`` Protocol.
    """

    default_converters: Dict[str, Type[BaseConverter]] = {
        "regex": RegexConverter,
        "port": PortConverter,
    }

    url_map: Map
    dispatcher: Dispatcher[E]

    def __init__(
        self, dispatcher: Dispatcher[E] = None, converters: Mapping[str, Type[BaseConverter]] = None
    ):
        if converters is None:
            converters = dict(self.default_converters)
        else:
            converters = {**self.default_converters, **converters}

        self.url_map = Map(
            host_matching=True,
            strict_slashes=False,
            converters=converters,
            redirect_defaults=False,
        )
        self.dispatcher = dispatcher or call_endpoint
        self._mutex = threading.RLock()

    @overload
    def add(
        self,
        path: str,
        endpoint: E,
        host: Optional[str] = None,
        methods: Optional[Iterable[str]] = None,
        **kwargs,
    ) -> Rule:
        """
        Creates a new Rule from the given parameters and adds it to the URL Map.

        TODO: many callers still expect ``add`` to return a single rule rather than a list, but it would be better to
         homogenize the API and make every method return a list.

        :param path: the path pattern to match. This path rule, in contrast to the default behavior of Werkzeug, will be
                        matched against the raw / original (potentially URL-encoded) path.
        :param endpoint: the endpoint to invoke
        :param host: an optional host matching pattern. if not pattern is given, the rule matches any host
        :param methods: the allowed HTTP verbs for this rule
        :param kwargs: any other argument that can be passed to ``werkzeug.routing.Rule``
        :return: the rule that was created
        """
        ...

    @overload
    def add(self, fn: _RouteEndpoint) -> List[Rule]:
        """
        Adds a RouteEndpoint (typically a function decorated with ``@route``) as a rule to the router.

        :param fn: the RouteEndpoint function
        :return: the rules that were added. for this operation, only one rule will be in the list
        """
        ...

    @overload
    def add(self, rule_factory: RuleFactory) -> List[Rule]:
        """
        Adds a ``Rule`` or the rules created by a ``RuleFactory`` to the given router. It passes the rules down to
        the underlying Werkzeug ``Map``, but also returns the created Rules.

        :param rule_factory: a `Rule` or ``RuleFactory`
        :return: the rules that were added
        """
        ...

    @overload
    def add(self, obj: Any) -> List[Rule]:
        """
        Scans the given object for members that can be used as a `RouteEndpoint` and adds them to the router.

        :param obj: the object to scan
        :return: the rules that were added
        """
        ...

    @overload
    def add(self, routes: list[Union[_RouteEndpoint, RuleFactory, Any]]) -> List[Rule]:
        """
        Add multiple routes or rules to the router at once.

        :param routes: the objects to add as routes
        :return: the rules that were added
        """
        ...

    def add(self, *args, **kwargs) -> Union[Rule, List[Rule]]:
        """
        Creates a ``RuleAdapter`` and adds the generated rules to the router's Map.
        """
        rules = self._add_rules(RuleAdapter(*args, **kwargs))

        if "path" in kwargs or type(args[0]) is str:
            return rules[0]

        return rules

    def _add_rules(self, rule_factory: RuleFactory) -> List[Rule]:
        """
        Thread safe version of Werkzeug's ``Map.add``.

        :param rule_factory: the rule to add
        """
        with self._mutex:
            rules = []
            for rule in rule_factory.get_rules(self.url_map):
                rules.append(rule)

                if rule.host is None and self.url_map.host_matching:
                    # this creates a "match any" rule, and will put the value of the host
                    # into the variable "__host__"
                    rule.host = "<__host__>"

                self.url_map.add(rule)

            return rules

    def add_rule(self, rule: RuleFactory):
        """
        Thread safe version of Werkzeug's ``Map.add``. This can be used as low-level method to pass a rule directly to
        the Werkzeug URL map without any manipulation or manual creation of the rule, which ``add`` does.

        :param rule: the rule to add
        """
        with self._mutex:
            self.url_map.add(rule)

    def remove(self, rules: Union[Rule, Iterable[Rule]]):
        """
        Removes a single Rule from the Router.

        **Caveat**: This is an expensive operation. Removing rules from a URL Map is intentionally not supported by
        werkzeug due to issues with thread safety, see https://github.com/pallets/werkzeug/issues/796, and because
        using a lock in ``match`` would be too expensive. However, some services that use Routers for routing
        internal resources need to be able to remove rules when those resources are removed. So to remove rules we
        create a new Map without that rule. This will not prevent the rules from dispatching until the Map has been
        completely constructed.

        :param rules: the Rule or rules to remove that were previously returned by ``add``.
        """
        if isinstance(rules, Rule):
            self._remove_rules([rules])
        else:
            self._remove_rules(rules)

    def _remove_rules(self, rules: Iterable[Rule]):
        """
        Removes a set of Rules from the Router.

        **Caveat**: This is an expensive operation. Removing rules from a URL Map is intentionally not supported by
        werkzeug due to issues with thread safety, see https://github.com/pallets/werkzeug/issues/796, and because
        using a lock in ``match`` would be too expensive. However, some services that use Routers for routing
        internal resources need to be able to remove rules when those resources are removed. So to remove rules we
        create a new Map without that rule. This will not prevent the rules from dispatching until the Map has been
        completely constructed.

        :param rules: the list of Rule objects to remove that were previously returned by ``add``.
        """
        with self._mutex:
            old = self.url_map
            for r in rules:
                if r not in old._rules:
                    raise KeyError("no such rule")

            # collect all old rules that are not in the set of rules to remove
            new = _clone_map_without_rules(old)

            for old_rule in old.iter_rules():
                if old_rule in rules:
                    # this works even with copied rules because of the __eq__ implementation of Rule
                    continue

                new.add(old_rule.empty())
            self.url_map = new

    def dispatch(self, request: Request) -> Response:
        """
        Does the entire dispatching roundtrip, from matching the request to endpoints, and then invoking the endpoint
        using the configured dispatcher of the router. For more information on the matching behavior,
        see ``werkzeug.routing.MapAdapter.match()``.

        :param request: the HTTP request
        :return: the HTTP response
        """
        matcher = self.url_map.bind(server_name=request.host)
        # Match on the _raw_ path to ensure that converters (like "path") can extract the raw path.
        # f.e. router.add(/<path:path>, ProxyHandler(...))
        # If we would use the - already url-decoded - request.path here, a handler would not be able to access
        # the original (potentially URL-encoded) path.
        # As a consequence, rules need to match on URL-encoded URLs (f.e. use '%20' instead of ' ').
        handler, args = matcher.match(get_raw_path(request), method=request.method)
        args.pop("__host__", None)
        return self.dispatcher(request, handler, args)

    def route(
        self,
        path: str,
        host: Optional[str] = None,
        methods: Optional[Iterable[str]] = None,
        **kwargs,
    ) -> Callable[[E], _RouteEndpoint]:
        """
        Returns a ``route`` decorator and immediately adds it to the router instance. This effectively mimics flask's
        ``@app.route``.

        :param path: the path pattern to match
        :param host: an optional host matching pattern. if not pattern is given, the rule matches any host
        :param methods: the allowed HTTP verbs for this rule
        :param kwargs: any other argument that can be passed to ``werkzeug.routing.Rule``
        :return: the function endpoint wrapped as a ``_RouteEndpoint``
        """

        def wrapper(fn):
            r = route(path, host, methods, **kwargs)
            fn = r(fn)
            self.add(fn)
            return fn

        return wrapper


class RuleAdapter(RuleFactory):
    """
    Takes something that can also be passed to ``Router.add``, and exposes it as a ``RuleFactory`` that generates the
    appropriate Werkzeug rules. This can be used in combination with other rule factories like ``Submount``,
    and creates general compatibility with werkzeug rules. Here's an example::

        @route("/my_api", methods=["GET"])
        def do_get(request: Request, _args):
            # should be inherited
            return Response(f"{request.path}/do-get")

        def hello(request: Request, _args):
            return Response(f"hello world")

        router = Router()

        # base endpoints
        endpoints = RuleAdapter([
            do_get,
            RuleAdapter("/hello", hello)
        ])

        router.add([
            endpoints,
            Submount("/foo", [endpoints])
        ])

    """

    factory: RuleFactory
    """The underlying real rule factory."""

    @overload
    def __init__(
        self,
        path: str,
        endpoint: E,
        host: Optional[str] = None,
        methods: Optional[Iterable[str]] = None,
        **kwargs,
    ):
        """
        Basically a ``Rule``.

        :param path: the path pattern to match. This path rule, in contrast to the default behavior of Werkzeug, will be
                        matched against the raw / original (potentially URL-encoded) path.
        :param endpoint: the endpoint to invoke
        :param host: an optional host matching pattern. if not pattern is given, the rule matches any host
        :param methods: the allowed HTTP verbs for this rule
        :param kwargs: any other argument that can be passed to ``werkzeug.routing.Rule``
        """
        ...

    @overload
    def __init__(self, fn: _RouteEndpoint):
        """
        Takes a route endpoint (typically a function decorated with ``@route``) and adds it as ``EndpointRule``.

        :param fn: the RouteEndpoint function
        """
        ...

    @overload
    def __init__(self, rule_factory: RuleFactory):
        """
        Adds a ``Rule`` or the rules created by a ``RuleFactory`` to the given router. It passes the rules down to
        the underlying Werkzeug ``Map``, but also returns the created Rules.

        :param rule_factory: a `Rule` or ``RuleFactory`
        """
        ...

    @overload
    def __init__(self, obj: Any):
        """
        Scans the given object for members that can be used as a `RouteEndpoint` and adds them to the router.

        :param obj: the object to scan
        """
        ...

    @overload
    def __init__(self, rules: list[Union[_RouteEndpoint, RuleFactory, Any]]):
        """Add multiple rules at once"""
        ...

    def __init__(self, *args, **kwargs):
        """
        Dispatcher for overloaded ``__init__`` methods.
        """
        if "path" in kwargs or type(args[0]) is str:
            self.factory = _EndpointRule(*args, **kwargs)
        elif "fn" in kwargs or callable(args[0]):
            self.factory = _EndpointFunction(*args, **kwargs)
        elif "rule_factory" in kwargs:
            self.factory = kwargs["rule_factory"]
        elif isinstance(args[0], RuleFactory):
            self.factory = args[0]
        elif isinstance(args[0], list):
            self.factory = RuleGroup([RuleAdapter(rule) for rule in args[0]])
        else:
            self.factory = _EndpointsObject(*args, **kwargs)

    def get_rules(self, map: Map) -> t.Iterable[Rule]:
        yield from self.factory.get_rules(map)


class WithHost(RuleFactory):
    def __init__(self, host: str, rules: Iterable[RuleFactory]) -> None:
        self.host = host
        self.rules = rules

    def get_rules(self, map: Map) -> t.Iterator[Rule]:
        for rulefactory in self.rules:
            for rule in rulefactory.get_rules(map):
                rule = rule.empty()
                rule.host = self.host
                yield rule


class RuleGroup(RuleFactory):
    def __init__(self, rules: Iterable[RuleFactory]):
        self.rules = rules

    def get_rules(self, map: Map) -> t.Iterable[Rule]:
        for rule in self.rules:
            yield from rule.get_rules(map)


class _EndpointRule(RuleFactory, Generic[E]):
    """
    Generates default werkzeug ``Rule`` object with the given attributes. Additionally, it makes sure that
    the generated rule always has a default host value, if the map has host matching enabled. Specifically,
    it adds the well-known placeholder ``<__host__>``, which is later stripped out of the request arguments
    when dispatching to the endpoint. This ensures compatibility of rule definitions across routers that
    have host matching enabled or not.
    """

    def __init__(
        self,
        path: str,
        endpoint: E,
        host: Optional[str] = None,
        methods: Optional[Iterable[str]] = None,
        **kwargs,
    ):
        self.path = path
        self.endpoint = endpoint
        self.host = host
        self.methods = methods
        self.kwargs = kwargs

    def get_rules(self, map: Map) -> t.Iterable[Rule]:
        host = self.host

        if host is None and map.host_matching:
            # this creates a "match any" rule, and will put the value of the host
            # into the variable "__host__"
            host = "<__host__>"

        # the typing for endpoint is a str, but the doc states it can be any value,
        # however then the redirection URL building will not work
        rule = Rule(
            self.path, endpoint=self.endpoint, methods=self.methods, host=host, **self.kwargs
        )
        yield rule


class _EndpointFunction(RuleFactory):
    """
    Internal rule factory that generates router Rules from ``@route`` annotated functions, or anything else
    that can be interpreted as a ``_RouteEndpoint``. It extracts the rule attributes from the
    ``_RuleAttributes`` attribute defined by ``_RouteEndpoint``. Example::

        @route("/my_api", methods=["GET"])
        def do_get(request: Request, _args):
            # should be inherited
            return Response(f"{request.path}/do-get")

        router.add(do_get)  # <- will use an _EndpointFunction RuleFactory.
    """

    def __init__(self, fn: _RouteEndpoint):
        self.fn = fn

    def get_rules(self, map: Map) -> t.Iterable[Rule]:
        attrs: list[_RuleAttributes] = self.fn.rule_attributes
        for attr in attrs:
            yield from _EndpointRule(
                path=attr.path,
                endpoint=self.fn,
                host=attr.host,
                methods=attr.methods,
                **attr.kwargs,
            ).get_rules(map)


class _EndpointsObject(RuleFactory):
    """
    Scans the given object for members that can be used as a `RouteEndpoint` and yields them as rules.
    """

    def __init__(self, obj: object):
        self.obj = obj

    def get_rules(self, map: Map) -> t.Iterable[Rule]:
        endpoints: list[_RouteEndpoint] = []

        members = inspect.getmembers(self.obj)
        for _, member in members:
            if hasattr(member, "rule_attributes"):
                endpoints.append(member)

        # make sure rules with "HEAD" are added first, otherwise werkzeug would let any "GET" rule would overwrite them.
        for endpoint in endpoints:
            for attr in endpoint.rule_attributes:
                if attr.methods and "HEAD" in attr.methods:
                    yield from _EndpointRule(
                        path=attr.path,
                        endpoint=endpoint,
                        host=attr.host,
                        methods=attr.methods,
                        **attr.kwargs,
                    ).get_rules(map)

        for endpoint in endpoints:
            for attr in endpoint.rule_attributes:
                if not attr.methods or "HEAD" not in attr.methods:
                    yield from _EndpointRule(
                        path=attr.path,
                        endpoint=endpoint,
                        host=attr.host,
                        methods=attr.methods,
                        **attr.kwargs,
                    ).get_rules(map)
