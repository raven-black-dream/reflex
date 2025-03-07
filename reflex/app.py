"""The main Reflex app."""
from __future__ import annotations

import asyncio
import inspect
import os
from multiprocessing.pool import ThreadPool
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Coroutine,
    Dict,
    List,
    Optional,
    Type,
    Union,
)

from fastapi import FastAPI, UploadFile
from fastapi.middleware import cors
from rich.progress import MofNCompleteColumn, Progress, TimeElapsedColumn
from socketio import ASGIApp, AsyncNamespace, AsyncServer
from starlette_admin.contrib.sqla.admin import Admin
from starlette_admin.contrib.sqla.view import ModelView

from reflex import constants
from reflex.admin import AdminDash
from reflex.base import Base
from reflex.compiler import compiler
from reflex.compiler import utils as compiler_utils
from reflex.components import connection_modal
from reflex.components.component import Component, ComponentStyle
from reflex.components.layout.fragment import Fragment
from reflex.components.navigation.client_side_routing import (
    Default404Page,
    wait_for_client_redirect,
)
from reflex.config import get_config
from reflex.event import Event, EventHandler, EventSpec
from reflex.middleware import HydrateMiddleware, Middleware
from reflex.model import Model
from reflex.page import (
    DECORATED_PAGES,
)
from reflex.route import (
    catchall_in_route,
    catchall_prefix,
    get_route_args,
    verify_route_validity,
)
from reflex.state import DefaultState, State, StateManager, StateUpdate
from reflex.utils import console, format, prerequisites, types

# Define custom types.
ComponentCallable = Callable[[], Component]
Reducer = Callable[[Event], Coroutine[Any, Any, StateUpdate]]


def default_overlay_component() -> Component:
    """Default overlay_component attribute for App.

    Returns:
        The default overlay_component, which is a connection_modal.
    """
    return connection_modal()


class App(Base):
    """A Reflex application."""

    # A map from a page route to the component to render.
    pages: Dict[str, Component] = {}

    # A list of URLs to stylesheets to include in the app.
    stylesheets: List[str] = []

    # The backend API object.
    api: FastAPI = None  # type: ignore

    # The Socket.IO AsyncServer.
    sio: Optional[AsyncServer] = None

    # The socket app.
    socket_app: Optional[ASGIApp] = None

    # The state class to use for the app.
    state: Type[State] = DefaultState

    # Class to manage many client states.
    state_manager: StateManager = StateManager()

    # The styling to apply to each component.
    style: ComponentStyle = {}

    # Middleware to add to the app.
    middleware: List[Middleware] = []

    # List of event handlers to trigger when a page loads.
    load_events: Dict[str, List[Union[EventHandler, EventSpec]]] = {}

    # Admin dashboard
    admin_dash: Optional[AdminDash] = None

    # The async server name space
    event_namespace: Optional[AsyncNamespace] = None

    # A component that is present on every page.
    overlay_component: Optional[
        Union[Component, ComponentCallable]
    ] = default_overlay_component

    def __init__(self, *args, **kwargs):
        """Initialize the app.

        Args:
            *args: Args to initialize the app with.
            **kwargs: Kwargs to initialize the app with.

        Raises:
            ValueError: If the event namespace is not provided in the config.
                        Also, if there are multiple client subclasses of rx.State(Subclasses of rx.State should consist
                        of the DefaultState and the client app state).
        """
        if "connect_error_component" in kwargs:
            raise ValueError(
                "`connect_error_component` is deprecated, use `overlay_component` instead"
            )
        super().__init__(*args, **kwargs)
        state_subclasses = State.__subclasses__()
        inferred_state = state_subclasses[-1]
        is_testing_env = constants.PYTEST_CURRENT_TEST in os.environ

        # Special case to allow test cases have multiple subclasses of rx.State.
        if not is_testing_env:
            # Only the default state and the client state should be allowed as subclasses.
            if len(state_subclasses) > 2:
                raise ValueError(
                    "rx.State has been subclassed multiple times. Only one subclass is allowed"
                )

            # verify that provided state is valid
            if self.state not in [DefaultState, inferred_state]:
                console.warn(
                    f"Using substate ({self.state.__name__}) as root state in `rx.App` is currently not supported."
                    f" Defaulting to root state: ({inferred_state.__name__})"
                )
            self.state = inferred_state
        # Get the config
        config = get_config()

        # Add middleware.
        self.middleware.append(HydrateMiddleware())

        # Set up the state manager.
        self.state_manager.setup(state=self.state)

        # Set up the API.
        self.api = FastAPI()
        self.add_cors()
        self.add_default_endpoints()

        # Set up the Socket.IO AsyncServer.
        self.sio = AsyncServer(
            async_mode="asgi",
            cors_allowed_origins="*"
            if config.cors_allowed_origins == ["*"]
            else config.cors_allowed_origins,
            cors_credentials=True,
            max_http_buffer_size=constants.POLLING_MAX_HTTP_BUFFER_SIZE,
            ping_interval=constants.PING_INTERVAL,
            ping_timeout=constants.PING_TIMEOUT,
        )

        # Create the socket app. Note event endpoint constant replaces the default 'socket.io' path.
        self.socket_app = ASGIApp(self.sio, socketio_path="")
        namespace = config.get_event_namespace()

        if not namespace:
            raise ValueError("event namespace must be provided in the config.")

        # Create the event namespace and attach the main app. Not related to any paths.
        self.event_namespace = EventNamespace(namespace, self)

        # Register the event namespace with the socket.
        self.sio.register_namespace(self.event_namespace)
        # Mount the socket app with the API.
        self.api.mount(str(constants.Endpoint.EVENT), self.socket_app)

        # Set up the admin dash.
        self.setup_admin_dash()

        # If a State is not used and no overlay_component is specified, do not render the connection modal
        if (
            self.state is DefaultState
            and self.overlay_component is default_overlay_component
        ):
            self.overlay_component = None

    def __repr__(self) -> str:
        """Get the string representation of the app.

        Returns:
            The string representation of the app.
        """
        return f"<App state={self.state.__name__}>"

    def __call__(self) -> FastAPI:
        """Run the backend api instance.

        Returns:
            The backend api.
        """
        return self.api

    def add_default_endpoints(self):
        """Add the default endpoints."""
        # To test the server.
        self.api.get(str(constants.Endpoint.PING))(ping)

        # To upload files.
        self.api.post(str(constants.Endpoint.UPLOAD))(upload(self))

    def add_cors(self):
        """Add CORS middleware to the app."""
        self.api.add_middleware(
            cors.CORSMiddleware,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            allow_origins=["*"],
        )

    async def preprocess(self, state: State, event: Event) -> StateUpdate | None:
        """Preprocess the event.

        This is where middleware can modify the event before it is processed.
        Each middleware is called in the order it was added to the app.

        If a middleware returns an update, the event is not processed and the
        update is returned.

        Args:
            state: The state to preprocess.
            event: The event to preprocess.

        Returns:
            An optional state to return.
        """
        for middleware in self.middleware:
            if asyncio.iscoroutinefunction(middleware.preprocess):
                out = await middleware.preprocess(app=self, state=state, event=event)  # type: ignore
            else:
                out = middleware.preprocess(app=self, state=state, event=event)  # type: ignore
            if out is not None:
                return out  # type: ignore

    async def postprocess(
        self, state: State, event: Event, update: StateUpdate
    ) -> StateUpdate:
        """Postprocess the event.

        This is where middleware can modify the delta after it is processed.
        Each middleware is called in the order it was added to the app.

        Args:
            state: The state to postprocess.
            event: The event to postprocess.
            update: The current state update.

        Returns:
            The state update to return.
        """
        for middleware in self.middleware:
            if asyncio.iscoroutinefunction(middleware.postprocess):
                out = await middleware.postprocess(
                    app=self, state=state, event=event, update=update  # type: ignore
                )
            else:
                out = middleware.postprocess(
                    app=self, state=state, event=event, update=update  # type: ignore
                )
            if out is not None:
                return out  # type: ignore
        return update

    def add_middleware(self, middleware: Middleware, index: int | None = None):
        """Add middleware to the app.

        Args:
            middleware: The middleware to add.
            index: The index to add the middleware at.
        """
        if index is None:
            self.middleware.append(middleware)
        else:
            self.middleware.insert(index, middleware)

    @staticmethod
    def _generate_component(component: Component | ComponentCallable) -> Component:
        """Generate a component from a callable.

        Args:
            component: The component function to call or Component to return as-is.

        Returns:
            The generated component.

        Raises:
            TypeError: When an invalid component function is passed.
        """
        try:
            return component if isinstance(component, Component) else component()
        except TypeError as e:
            message = str(e)
            if "BaseVar" in message or "ComputedVar" in message:
                raise TypeError(
                    "You may be trying to use an invalid Python function on a state var. "
                    "When referencing a var inside your render code, only limited var operations are supported. "
                    "See the var operation docs here: https://reflex.dev/docs/state/vars/#var-operations"
                ) from e
            raise e

    def add_page(
        self,
        component: Component | ComponentCallable,
        route: str | None = None,
        title: str = constants.DEFAULT_TITLE,
        description: str = constants.DEFAULT_DESCRIPTION,
        image=constants.DEFAULT_IMAGE,
        on_load: EventHandler
        | EventSpec
        | list[EventHandler | EventSpec]
        | None = None,
        meta: list[dict[str, str]] = constants.DEFAULT_META_LIST,
        script_tags: list[Component] | None = None,
    ):
        """Add a page to the app.

        If the component is a callable, by default the route is the name of the
        function. Otherwise, a route must be provided.

        Args:
            component: The component to display at the page.
            route: The route to display the component at.
            title: The title of the page.
            description: The description of the page.
            image: The image to display on the page.
            on_load: The event handler(s) that will be called each time the page load.
            meta: The metadata of the page.
            script_tags: List of script tags to be added to component
        """
        # If the route is not set, get it from the callable.
        if route is None:
            assert isinstance(
                component, Callable
            ), "Route must be set if component is not a callable."
            # Format the route.
            route = format.format_route(component.__name__)
        else:
            route = format.format_route(route, format_case=False)

        # Check if the route given is valid
        verify_route_validity(route)

        # Apply dynamic args to the route.
        self.state.setup_dynamic_args(get_route_args(route))

        # Generate the component if it is a callable.
        component = self._generate_component(component)

        # Wrap the component in a fragment with optional overlay.
        if self.overlay_component is not None:
            component = Fragment.create(
                self._generate_component(self.overlay_component),
                component,
            )
        else:
            component = Fragment.create(component)

        # Add meta information to the component.
        compiler_utils.add_meta(
            component,
            title=title,
            image=image,
            description=description,
            meta=meta,
        )

        # Add script tags if given
        if script_tags:
            component.children.extend(script_tags)

        # Add the page.
        self._check_routes_conflict(route)
        self.pages[route] = component

        # Add the load events.
        if on_load:
            if not isinstance(on_load, list):
                on_load = [on_load]
            self.load_events[route] = on_load

    def get_load_events(self, route: str) -> list[EventHandler | EventSpec]:
        """Get the load events for a route.

        Args:
            route: The route to get the load events for.

        Returns:
            The load events for the route.
        """
        route = route.lstrip("/")
        if route == "":
            route = constants.INDEX_ROUTE
        return self.load_events.get(route, [])

    def _check_routes_conflict(self, new_route: str):
        """Verify if there is any conflict between the new route and any existing route.

        Based on conflicts that NextJS would throw if not intercepted.

        Raises:
            ValueError: exception showing which conflict exist with the route to be added

        Args:
            new_route: the route being newly added.
        """
        newroute_catchall = catchall_in_route(new_route)
        if not newroute_catchall:
            return

        for route in self.pages:
            route = "" if route == "index" else route

            if new_route.startswith(f"{route}/[[..."):
                raise ValueError(
                    f"You cannot define a route with the same specificity as a optional catch-all route ('{route}' and '{new_route}')"
                )

            route_catchall = catchall_in_route(route)
            if (
                route_catchall
                and newroute_catchall
                and catchall_prefix(route) == catchall_prefix(new_route)
            ):
                raise ValueError(
                    f"You cannot use multiple catchall for the same dynamic route ({route} !== {new_route})"
                )

    def add_custom_404_page(
        self,
        component: Component | ComponentCallable | None = None,
        title: str = constants.TITLE_404,
        image: str = constants.FAVICON_404,
        description: str = constants.DESCRIPTION_404,
        on_load: EventHandler
        | EventSpec
        | list[EventHandler | EventSpec]
        | None = None,
        meta: list[dict[str, str]] = constants.DEFAULT_META_LIST,
    ):
        """Define a custom 404 page for any url having no match.

        If there is no page defined on 'index' route, add the 404 page to it.
        If there is no global catchall defined, add the 404 page with a catchall

        Args:
            component: The component to display at the page.
            title: The title of the page.
            description: The description of the page.
            image: The image to display on the page.
            on_load: The event handler(s) that will be called each time the page load.
            meta: The metadata of the page.
        """
        if component is None:
            component = Default404Page.create()
        self.add_page(
            component=wait_for_client_redirect(self._generate_component(component)),
            route=constants.SLUG_404,
            title=title or constants.TITLE_404,
            image=image or constants.FAVICON_404,
            description=description or constants.DESCRIPTION_404,
            on_load=on_load,
            meta=meta,
        )

    def setup_admin_dash(self):
        """Setup the admin dash."""
        # Get the admin dash.
        admin_dash = self.admin_dash

        if admin_dash and admin_dash.models:
            # Build the admin dashboard
            admin = (
                admin_dash.admin
                if admin_dash.admin
                else Admin(
                    engine=Model.get_db_engine(),
                    title="Reflex Admin Dashboard",
                    logo_url="https://reflex.dev/Reflex.svg",
                )
            )

            for model in admin_dash.models:
                view = admin_dash.view_overrides.get(model, ModelView)
                admin.add_view(view(model))

            admin.mount_to(self.api)

    def get_frontend_packages(self, imports: Dict[str, str]):
        """Gets the frontend packages to be installed and filters out the unnecessary ones.

        Args:
            imports: A dictionary containing the imports used in the current page.

        Example:
            >>> get_frontend_packages({"react": "16.14.0", "react-dom": "16.14.0"})
        """
        page_imports = {
            i
            for i in imports
            if i not in compiler.DEFAULT_IMPORTS.keys()
            and i != "focus-visible/dist/focus-visible"
            and "next" not in i
            and not i.startswith("/")
            and i != ""
        }
        frontend_packages = get_config().frontend_packages
        _frontend_packages = []
        for package in frontend_packages:
            if package in (get_config().tailwind or {}).get("plugins", []):  # type: ignore
                console.warn(
                    f"Tailwind packages are inferred from 'plugins', remove `{package}` from `frontend_packages`"
                )
                continue
            if package in page_imports:
                console.warn(
                    f"React packages and their dependencies are inferred from Component.library and Component.lib_dependencies, remove `{package}` from `frontend_packages`"
                )
                continue
            _frontend_packages.append(package)
        page_imports.update(_frontend_packages)
        prerequisites.install_frontend_packages(page_imports)

    def compile(self):
        """Compile the app and output it to the pages folder."""
        if os.environ.get(constants.SKIP_COMPILE_ENV_VAR) == "yes":
            return
        # Create a progress bar.
        progress = Progress(
            *Progress.get_default_columns()[:-1],
            MofNCompleteColumn(),
            TimeElapsedColumn(),
        )

        for render, kwargs in DECORATED_PAGES:
            self.add_page(render, **kwargs)

        # Render a default 404 page if the user didn't supply one
        if constants.SLUG_404 not in self.pages:
            self.add_custom_404_page()

        task = progress.add_task("Compiling: ", total=len(self.pages))
        # TODO: include all work done in progress indicator, not just self.pages

        # Get the env mode.
        config = get_config()

        # Store the compile results.
        compile_results = []

        # Compile the pages in parallel.
        custom_components = set()
        # TODO Anecdotally, processes=2 works 10% faster (cpu_count=12)
        thread_pool = ThreadPool()
        all_imports = {}
        with progress:
            for route, component in self.pages.items():
                # TODO: this progress does not reflect actual threaded task completion
                progress.advance(task)
                component.add_style(self.style)
                compile_results.append(
                    thread_pool.apply_async(
                        compiler.compile_page,
                        args=(
                            route,
                            component,
                            self.state,
                        ),
                    )
                )
                # add component.get_imports() to all_imports
                all_imports.update(component.get_imports())

                # Add the custom components from the page to the set.
                custom_components |= component.get_custom_components()

        thread_pool.close()
        thread_pool.join()

        # Get the results.
        compile_results = [result.get() for result in compile_results]

        # TODO the compile tasks below may also benefit from parallelization too

        # Compile the custom components.
        compile_results.append(compiler.compile_components(custom_components))

        # Iterate through all the custom components and add their imports to the all_imports
        for component in custom_components:
            all_imports.update(component.get_imports())

        # Compile the root document with base styles and fonts
        compile_results.append(compiler.compile_document_root(self.stylesheets))

        # Compile the theme.
        compile_results.append(compiler.compile_theme(self.style))

        # Compile the contexts.
        compile_results.append(compiler.compile_contexts(self.state))

        # Compile the Tailwind config.
        if config.tailwind is not None:
            config.tailwind["content"] = config.tailwind.get(
                "content", constants.TAILWIND_CONTENT
            )
            compile_results.append(compiler.compile_tailwind(config.tailwind))

        # Empty the .web pages directory
        compiler.purge_web_pages_dir()

        # install frontend packages
        self.get_frontend_packages(all_imports)

        # Write the pages at the end to trigger the NextJS hot reload only once.
        thread_pool = ThreadPool()
        for output_path, code in compile_results:
            thread_pool.apply_async(compiler_utils.write_page, args=(output_path, code))
        thread_pool.close()
        thread_pool.join()


async def process(
    app: App, event: Event, sid: str, headers: Dict, client_ip: str
) -> AsyncIterator[StateUpdate]:
    """Process an event.

    Args:
        app: The app to process the event for.
        event: The event to process.
        sid: The Socket.IO session id.
        headers: The client headers.
        client_ip: The client_ip.

    Yields:
        The state updates after processing the event.
    """
    # Get the state for the session.
    state = app.state_manager.get_state(event.token)

    # Add request data to the state.
    router_data = event.router_data
    router_data.update(
        {
            constants.RouteVar.QUERY: format.format_query_params(event.router_data),
            constants.RouteVar.CLIENT_TOKEN: event.token,
            constants.RouteVar.SESSION_ID: sid,
            constants.RouteVar.HEADERS: headers,
            constants.RouteVar.CLIENT_IP: client_ip,
        }
    )
    # re-assign only when the value is different
    if state.router_data != router_data:
        # assignment will recurse into substates and force recalculation of
        # dependent ComputedVar (dynamic route variables)
        state.router_data = router_data

    # Preprocess the event.
    update = await app.preprocess(state, event)

    # If there was an update, yield it.
    if update is not None:
        yield update

    # Only process the event if there is no update.
    else:
        # Process the event.
        async for update in state._process(event):
            # Postprocess the event.
            update = await app.postprocess(state, event, update)

            # Yield the update.
            yield update

    # Set the state for the session.
    app.state_manager.set_state(event.token, state)


async def ping() -> str:
    """Test API endpoint.

    Returns:
        The response.
    """
    return "pong"


def upload(app: App):
    """Upload a file.

    Args:
        app: The app to upload the file for.

    Returns:
        The upload function.
    """

    async def upload_file(files: List[UploadFile]):
        """Upload a file.

        Args:
            files: The file(s) to upload.

        Raises:
            ValueError: if there are no args with supported annotation.
        """
        assert files[0].filename is not None
        token, handler = files[0].filename.split(":")[:2]
        for file in files:
            assert file.filename is not None
            file.filename = file.filename.split(":")[-1]
        # Get the state for the session.
        state = app.state_manager.get_state(token)
        # get the current session ID
        sid = state.get_sid()
        # get the current state(parent state/substate)
        path = handler.split(".")[:-1]
        current_state = state.get_substate(path)
        handler_upload_param = ()

        # get handler function
        func = getattr(current_state, handler.split(".")[-1])

        # check if there exists any handler args with annotation, List[UploadFile]
        for k, v in inspect.getfullargspec(
            func.fn if isinstance(func, EventHandler) else func
        ).annotations.items():
            if types.is_generic_alias(v) and types._issubclass(
                v.__args__[0], UploadFile
            ):
                handler_upload_param = (k, v)
                break

        if not handler_upload_param:
            raise ValueError(
                f"`{handler}` handler should have a parameter annotated as List["
                f"rx.UploadFile]"
            )

        event = Event(
            token=token,
            name=handler,
            payload={handler_upload_param[0]: files},
        )
        async for update in state._process(event):
            # Postprocess the event.
            update = await app.postprocess(state, event, update)
            # Send update to client
            await asyncio.create_task(
                app.event_namespace.emit(str(constants.SocketEvent.EVENT), update.json(), to=sid)  # type: ignore
            )
        # Set the state for the session.
        app.state_manager.set_state(event.token, state)

    return upload_file


class EventNamespace(AsyncNamespace):
    """The event namespace."""

    # The application object.
    app: App

    def __init__(self, namespace: str, app: App):
        """Initialize the event namespace.

        Args:
            namespace: The namespace.
            app: The application object.
        """
        super().__init__(namespace)
        self.app = app

    def on_connect(self, sid, environ):
        """Event for when the websocket is connected.

        Args:
            sid: The Socket.IO session id.
            environ: The request information, including HTTP headers.
        """
        pass

    def on_disconnect(self, sid):
        """Event for when the websocket disconnects.

        Args:
            sid: The Socket.IO session id.
        """
        pass

    async def on_event(self, sid, data):
        """Event for receiving front-end websocket events.

        Args:
            sid: The Socket.IO session id.
            data: The event data.
        """
        # Get the event.
        event = Event.parse_raw(data)

        # Get the event environment.
        assert self.app.sio is not None
        environ = self.app.sio.get_environ(sid, self.namespace)
        assert environ is not None

        # Get the client headers.
        headers = {
            k.decode("utf-8"): v.decode("utf-8")
            for (k, v) in environ["asgi.scope"]["headers"]
        }

        # Get the client IP
        client_ip = environ["REMOTE_ADDR"]

        # Process the events.
        async for update in process(self.app, event, sid, headers, client_ip):
            # Emit the event.
            await asyncio.create_task(
                self.emit(str(constants.SocketEvent.EVENT), update.json(), to=sid)
            )

    async def on_ping(self, sid):
        """Event for testing the API endpoint.

        Args:
            sid: The Socket.IO session id.
        """
        # Emit the test event.
        await self.emit(str(constants.SocketEvent.PING), "pong", to=sid)
