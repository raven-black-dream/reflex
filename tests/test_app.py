from __future__ import annotations

import io
import os.path
import sys
from typing import List, Tuple, Type

if sys.version_info.major >= 3 and sys.version_info.minor > 7:
    from unittest.mock import AsyncMock  # type: ignore
else:
    # python 3.7 doesn't ship with unittest.mock
    from asynctest import CoroutineMock as AsyncMock
import pytest
import sqlmodel
from fastapi import UploadFile
from starlette_admin.auth import AuthProvider
from starlette_admin.contrib.sqla.admin import Admin
from starlette_admin.contrib.sqla.view import ModelView

from reflex import AdminDash, constants
from reflex.app import (
    App,
    ComponentCallable,
    DefaultState,
    default_overlay_component,
    process,
    upload,
)
from reflex.components import Box, Component, Cond, Fragment, Text
from reflex.event import Event, get_hydrate_event
from reflex.middleware import HydrateMiddleware
from reflex.model import Model
from reflex.state import State, StateUpdate
from reflex.style import Style
from reflex.utils import format
from reflex.vars import ComputedVar


@pytest.fixture
def index_page():
    """An index page.

    Returns:
        The index page.
    """

    def index():
        return Box.create("Index")

    return index


@pytest.fixture
def about_page():
    """An about page.

    Returns:
        The about page.
    """

    def about():
        return Box.create("About")

    return about


@pytest.fixture()
def test_state() -> Type[State]:
    """A default state.

    Returns:
        A default state.
    """

    class TestState(State):
        var: int

    return TestState


@pytest.fixture()
def redundant_test_state() -> Type[State]:
    """A default state.

    Returns:
        A default state.
    """

    class RedundantTestState(State):
        var: int

    return RedundantTestState


@pytest.fixture(scope="session")
def test_model() -> Type[Model]:
    """A default model.

    Returns:
        A default model.
    """

    class TestModel(Model, table=True):  # type: ignore
        pass

    return TestModel


@pytest.fixture(scope="session")
def test_model_auth() -> Type[Model]:
    """A default model.

    Returns:
        A default model.
    """

    class TestModelAuth(Model, table=True):  # type: ignore
        """A test model with auth."""

        pass

    return TestModelAuth


@pytest.fixture()
def test_get_engine():
    """A default database engine.

    Returns:
        A default database engine.
    """
    enable_admin = True
    url = "sqlite:///test.db"
    return sqlmodel.create_engine(
        url,
        echo=False,
        connect_args={"check_same_thread": False} if enable_admin else {},
    )


@pytest.fixture()
def test_custom_auth_admin() -> Type[AuthProvider]:
    """A default auth provider.

    Returns:
        A default default auth provider.
    """

    class TestAuthProvider(AuthProvider):
        """A test auth provider."""

        login_path: str = "/login"
        logout_path: str = "/logout"

        def login(self):
            """Login."""
            pass

        def is_authenticated(self):
            """Is authenticated."""
            pass

        def get_admin_user(self):
            """Get admin user."""
            pass

        def logout(self):
            """Logout."""
            pass

    return TestAuthProvider


def test_default_app(app: App):
    """Test creating an app with no args.

    Args:
        app: The app to test.
    """
    assert app.state() == DefaultState()
    assert app.middleware == [HydrateMiddleware()]
    assert app.style == Style()
    assert app.admin_dash is None


def test_multiple_states_error(monkeypatch, test_state, redundant_test_state):
    """Test that an error is thrown when multiple classes subclass rx.State.

    Args:
        monkeypatch: Pytest monkeypatch object.
        test_state: A test state subclassing rx.State.
        redundant_test_state: Another test state subclassing rx.State.
    """
    monkeypatch.delenv(constants.PYTEST_CURRENT_TEST)
    with pytest.raises(ValueError):
        App()


def test_add_page_default_route(app: App, index_page, about_page):
    """Test adding a page to an app.

    Args:
        app: The app to test.
        index_page: The index page.
        about_page: The about page.
    """
    assert app.pages == {}
    app.add_page(index_page)
    assert set(app.pages.keys()) == {"index"}
    app.add_page(about_page)
    assert set(app.pages.keys()) == {"index", "about"}


def test_add_page_set_route(app: App, index_page, windows_platform: bool):
    """Test adding a page to an app.

    Args:
        app: The app to test.
        index_page: The index page.
        windows_platform: Whether the system is windows.
    """
    route = "test" if windows_platform else "/test"
    assert app.pages == {}
    app.add_page(index_page, route=route)
    assert set(app.pages.keys()) == {"test"}


def test_add_page_set_route_dynamic(app: App, index_page, windows_platform: bool):
    """Test adding a page with dynamic route variable to an app.

    Args:
        app: The app to test.
        index_page: The index page.
        windows_platform: Whether the system is windows.
    """
    route = "/test/[dynamic]"
    if windows_platform:
        route.lstrip("/").replace("/", "\\")
    assert app.pages == {}
    app.add_page(index_page, route=route)
    assert set(app.pages.keys()) == {"test/[dynamic]"}
    assert "dynamic" in app.state.computed_vars
    assert app.state.computed_vars["dynamic"].deps(objclass=DefaultState) == {
        constants.ROUTER_DATA
    }
    assert constants.ROUTER_DATA in app.state().computed_var_dependencies


def test_add_page_set_route_nested(app: App, index_page, windows_platform: bool):
    """Test adding a page to an app.

    Args:
        app: The app to test.
        index_page: The index page.
        windows_platform: Whether the system is windows.
    """
    route = "test\\nested" if windows_platform else "/test/nested"
    assert app.pages == {}
    app.add_page(index_page, route=route)
    assert set(app.pages.keys()) == {route.strip(os.path.sep)}


def test_initialize_with_admin_dashboard(test_model):
    """Test setting the admin dashboard of an app.

    Args:
        test_model: The default model.
    """
    app = App(admin_dash=AdminDash(models=[test_model]))
    assert app.admin_dash is not None
    assert len(app.admin_dash.models) > 0
    assert app.admin_dash.models[0] == test_model


def test_initialize_with_custom_admin_dashboard(
    test_get_engine,
    test_custom_auth_admin,
    test_model_auth,
):
    """Test setting the custom admin dashboard of an app.

    Args:
        test_get_engine: The default database engine.
        test_model_auth: The default model for an auth admin dashboard.
        test_custom_auth_admin: The custom auth provider.
    """
    custom_admin = Admin(engine=test_get_engine, auth_provider=test_custom_auth_admin)
    app = App(admin_dash=AdminDash(models=[test_model_auth], admin=custom_admin))
    assert app.admin_dash is not None
    assert app.admin_dash.admin is not None
    assert len(app.admin_dash.models) > 0
    assert app.admin_dash.models[0] == test_model_auth
    assert app.admin_dash.admin.auth_provider == test_custom_auth_admin


def test_initialize_admin_dashboard_with_view_overrides(test_model):
    """Test setting the admin dashboard of an app with view class overriden.

    Args:
        test_model: The default model.
    """

    class TestModelView(ModelView):
        pass

    app = App(
        admin_dash=AdminDash(
            models=[test_model], view_overrides={test_model: TestModelView}
        )
    )
    assert app.admin_dash is not None
    assert app.admin_dash.models == [test_model]
    assert app.admin_dash.view_overrides[test_model] == TestModelView


def test_initialize_with_state(test_state):
    """Test setting the state of an app.

    Args:
        test_state: The default state.
    """
    app = App(state=test_state)
    assert app.state == test_state

    # Get a state for a given token.
    token = "token"
    state = app.state_manager.get_state(token)
    assert isinstance(state, test_state)
    assert state.var == 0  # type: ignore


def test_set_and_get_state(test_state):
    """Test setting and getting the state of an app with different tokens.

    Args:
        test_state: The default state.
    """
    app = App(state=test_state)

    # Create two tokens.
    token1 = "token1"
    token2 = "token2"

    # Get the default state for each token.
    state1 = app.state_manager.get_state(token1)
    state2 = app.state_manager.get_state(token2)
    assert state1.var == 0  # type: ignore
    assert state2.var == 0  # type: ignore

    # Set the vars to different values.
    state1.var = 1
    state2.var = 2
    app.state_manager.set_state(token1, state1)
    app.state_manager.set_state(token2, state2)

    # Get the states again and check the values.
    state1 = app.state_manager.get_state(token1)
    state2 = app.state_manager.get_state(token2)
    assert state1.var == 1  # type: ignore
    assert state2.var == 2  # type: ignore


@pytest.mark.asyncio
async def test_dynamic_var_event(test_state):
    """Test that the default handler of a dynamic generated var
    works as expected.

    Args:
        test_state: State Fixture.
    """
    test_state = test_state()
    test_state.add_var("int_val", int, 0)
    result = await test_state._process(
        Event(
            token="fake-token",
            name="test_state.set_int_val",
            router_data={"pathname": "/", "query": {}},
            payload={"value": 50},
        )
    ).__anext__()
    assert result.delta == {"test_state": {"int_val": 50}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_tuples",
    [
        pytest.param(
            [
                (
                    "test_state.make_friend",
                    {"test_state": {"plain_friends": ["Tommy", "another-fd"]}},
                ),
                (
                    "test_state.change_first_friend",
                    {"test_state": {"plain_friends": ["Jenny", "another-fd"]}},
                ),
            ],
            id="append then __setitem__",
        ),
        pytest.param(
            [
                (
                    "test_state.unfriend_first_friend",
                    {"test_state": {"plain_friends": []}},
                ),
                (
                    "test_state.make_friend",
                    {"test_state": {"plain_friends": ["another-fd"]}},
                ),
            ],
            id="delitem then append",
        ),
        pytest.param(
            [
                (
                    "test_state.make_friends_with_colleagues",
                    {"test_state": {"plain_friends": ["Tommy", "Peter", "Jimmy"]}},
                ),
                (
                    "test_state.remove_tommy",
                    {"test_state": {"plain_friends": ["Peter", "Jimmy"]}},
                ),
                (
                    "test_state.remove_last_friend",
                    {"test_state": {"plain_friends": ["Peter"]}},
                ),
                (
                    "test_state.unfriend_all_friends",
                    {"test_state": {"plain_friends": []}},
                ),
            ],
            id="extend, remove, pop, clear",
        ),
        pytest.param(
            [
                (
                    "test_state.add_jimmy_to_second_group",
                    {
                        "test_state": {
                            "friends_in_nested_list": [["Tommy"], ["Jenny", "Jimmy"]]
                        }
                    },
                ),
                (
                    "test_state.remove_first_person_from_first_group",
                    {
                        "test_state": {
                            "friends_in_nested_list": [[], ["Jenny", "Jimmy"]]
                        }
                    },
                ),
                (
                    "test_state.remove_first_group",
                    {"test_state": {"friends_in_nested_list": [["Jenny", "Jimmy"]]}},
                ),
            ],
            id="nested list",
        ),
        pytest.param(
            [
                (
                    "test_state.add_jimmy_to_tommy_friends",
                    {"test_state": {"friends_in_dict": {"Tommy": ["Jenny", "Jimmy"]}}},
                ),
                (
                    "test_state.remove_jenny_from_tommy",
                    {"test_state": {"friends_in_dict": {"Tommy": ["Jimmy"]}}},
                ),
                (
                    "test_state.tommy_has_no_fds",
                    {"test_state": {"friends_in_dict": {"Tommy": []}}},
                ),
            ],
            id="list in dict",
        ),
    ],
)
async def test_list_mutation_detection__plain_list(
    event_tuples: List[Tuple[str, List[str]]], list_mutation_state: State
):
    """Test list mutation detection
    when reassignment is not explicitly included in the logic.

    Args:
        event_tuples: From parametrization.
        list_mutation_state: A state with list mutation features.
    """
    for event_name, expected_delta in event_tuples:
        result = await list_mutation_state._process(
            Event(
                token="fake-token",
                name=event_name,
                router_data={"pathname": "/", "query": {}},
                payload={},
            )
        ).__anext__()

        assert result.delta == expected_delta


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_tuples",
    [
        pytest.param(
            [
                (
                    "test_state.add_age",
                    {"test_state": {"details": {"name": "Tommy", "age": 20}}},
                ),
                (
                    "test_state.change_name",
                    {"test_state": {"details": {"name": "Jenny", "age": 20}}},
                ),
                (
                    "test_state.remove_last_detail",
                    {"test_state": {"details": {"name": "Jenny"}}},
                ),
            ],
            id="update then __setitem__",
        ),
        pytest.param(
            [
                (
                    "test_state.clear_details",
                    {"test_state": {"details": {}}},
                ),
                (
                    "test_state.add_age",
                    {"test_state": {"details": {"age": 20}}},
                ),
            ],
            id="delitem then update",
        ),
        pytest.param(
            [
                (
                    "test_state.add_age",
                    {"test_state": {"details": {"name": "Tommy", "age": 20}}},
                ),
                (
                    "test_state.remove_name",
                    {"test_state": {"details": {"age": 20}}},
                ),
                (
                    "test_state.pop_out_age",
                    {"test_state": {"details": {}}},
                ),
            ],
            id="add, remove, pop",
        ),
        pytest.param(
            [
                (
                    "test_state.remove_home_address",
                    {"test_state": {"address": [{}, {"work": "work address"}]}},
                ),
                (
                    "test_state.add_street_to_home_address",
                    {
                        "test_state": {
                            "address": [
                                {"street": "street address"},
                                {"work": "work address"},
                            ]
                        }
                    },
                ),
            ],
            id="dict in list",
        ),
        pytest.param(
            [
                (
                    "test_state.change_friend_name",
                    {
                        "test_state": {
                            "friend_in_nested_dict": {
                                "name": "Nikhil",
                                "friend": {"name": "Tommy"},
                            }
                        }
                    },
                ),
                (
                    "test_state.add_friend_age",
                    {
                        "test_state": {
                            "friend_in_nested_dict": {
                                "name": "Nikhil",
                                "friend": {"name": "Tommy", "age": 30},
                            }
                        }
                    },
                ),
                (
                    "test_state.remove_friend",
                    {"test_state": {"friend_in_nested_dict": {"name": "Nikhil"}}},
                ),
            ],
            id="nested dict",
        ),
    ],
)
async def test_dict_mutation_detection__plain_list(
    event_tuples: List[Tuple[str, List[str]]], dict_mutation_state: State
):
    """Test dict mutation detection
    when reassignment is not explicitly included in the logic.

    Args:
        event_tuples: From parametrization.
        dict_mutation_state: A state with dict mutation features.
    """
    for event_name, expected_delta in event_tuples:
        result = await dict_mutation_state._process(
            Event(
                token="fake-token",
                name=event_name,
                router_data={"pathname": "/", "query": {}},
                payload={},
            )
        ).__anext__()

        assert result.delta == expected_delta


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture, delta",
    [
        (
            "upload_state",
            {"file_upload_state": {"img_list": ["image1.jpg", "image2.jpg"]}},
        ),
        (
            "upload_sub_state",
            {
                "file_state.file_upload_state": {
                    "img_list": ["image1.jpg", "image2.jpg"]
                }
            },
        ),
        (
            "upload_grand_sub_state",
            {
                "base_file_state.file_sub_state.file_upload_state": {
                    "img_list": ["image1.jpg", "image2.jpg"]
                }
            },
        ),
    ],
)
async def test_upload_file(fixture, request, delta):
    """Test that file upload works correctly.

    Args:
        fixture: The state.
        request: Fixture request.
        delta: Expected delta
    """
    app = App(state=request.getfixturevalue(fixture))
    app.event_namespace.emit = AsyncMock()  # type: ignore
    current_state = app.state_manager.get_state("token")
    data = b"This is binary data"

    # Create a binary IO object and write data to it
    bio = io.BytesIO()
    bio.write(data)

    file1 = UploadFile(
        filename="token:file_upload_state.multi_handle_upload:True:image1.jpg",
        file=bio,
    )
    file2 = UploadFile(
        filename="token:file_upload_state.multi_handle_upload:True:image2.jpg",
        file=bio,
    )
    upload_fn = upload(app)
    await upload_fn([file1, file2])
    state_update = StateUpdate(delta=delta, events=[], final=True)

    app.event_namespace.emit.assert_called_with(  # type: ignore
        "event", state_update.json(), to=current_state.get_sid()
    )
    assert app.state_manager.get_state("token").dict()["img_list"] == [
        "image1.jpg",
        "image2.jpg",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture", ["upload_state", "upload_sub_state", "upload_grand_sub_state"]
)
async def test_upload_file_without_annotation(fixture, request):
    """Test that an error is thrown when there's no param annotated with rx.UploadFile or List[UploadFile].

    Args:
        fixture: The state.
        request: Fixture request.
    """
    data = b"This is binary data"

    # Create a binary IO object and write data to it
    bio = io.BytesIO()
    bio.write(data)

    app = App(state=request.getfixturevalue(fixture))

    file1 = UploadFile(
        filename="token:file_upload_state.handle_upload2:True:image1.jpg",
        file=bio,
    )
    file2 = UploadFile(
        filename="token:file_upload_state.handle_upload2:True:image2.jpg",
        file=bio,
    )
    fn = upload(app)
    with pytest.raises(ValueError) as err:
        await fn([file1, file2])
    assert (
        err.value.args[0]
        == "`file_upload_state.handle_upload2` handler should have a parameter annotated as List[rx.UploadFile]"
    )


class DynamicState(State):
    """State class for testing dynamic route var.

    This is defined at module level because event handlers cannot be addressed
    correctly when the class is defined as a local.

    There are several counters:
      * loaded: counts how many times `on_load` was triggered by the hydrate middleware
      * counter: counts how many times `on_counter` was triggered by a non-navigational event
          -> these events should NOT trigger reload or recalculation of router_data dependent vars
      * side_effect_counter: counts how many times a computed var was
        recalculated when the dynamic route var was dirty
    """

    loaded: int = 0
    counter: int = 0

    # side_effect_counter: int = 0

    def on_load(self):
        """Event handler for page on_load, should trigger for all navigation events."""
        self.loaded = self.loaded + 1

    def on_counter(self):
        """Increment the counter var."""
        self.counter = self.counter + 1

    @ComputedVar
    def comp_dynamic(self) -> str:
        """A computed var that depends on the dynamic var.

        Returns:
            same as self.dynamic
        """
        # self.side_effect_counter = self.side_effect_counter + 1
        return self.dynamic


@pytest.mark.asyncio
async def test_dynamic_route_var_route_change_completed_on_load(
    index_page,
    windows_platform: bool,
):
    """Create app with dynamic route var, and simulate navigation.

    on_load should fire, allowing any additional vars to be updated before the
    initial page hydrate.

    Args:
        index_page: The index page.
        windows_platform: Whether the system is windows.
    """
    arg_name = "dynamic"
    route = f"/test/[{arg_name}]"
    if windows_platform:
        route.lstrip("/").replace("/", "\\")
    app = App(state=DynamicState)
    assert arg_name not in app.state.vars
    app.add_page(index_page, route=route, on_load=DynamicState.on_load)  # type: ignore
    assert arg_name in app.state.vars
    assert arg_name in app.state.computed_vars
    assert app.state.computed_vars[arg_name].deps(objclass=DynamicState) == {
        constants.ROUTER_DATA
    }
    assert constants.ROUTER_DATA in app.state().computed_var_dependencies

    token = "mock_token"
    sid = "mock_sid"
    client_ip = "127.0.0.1"
    state = app.state_manager.get_state(token)
    assert state.dynamic == ""
    exp_vals = ["foo", "foobar", "baz"]

    def _event(name, val, **kwargs):
        return Event(
            token=kwargs.pop("token", token),
            name=name,
            router_data=kwargs.pop(
                "router_data", {"pathname": route, "query": {arg_name: val}}
            ),
            payload=kwargs.pop("payload", {}),
            **kwargs,
        )

    def _dynamic_state_event(name, val, **kwargs):
        return _event(
            name=format.format_event_handler(getattr(DynamicState, name)),  # type: ignore
            val=val,
            **kwargs,
        )

    for exp_index, exp_val in enumerate(exp_vals):
        hydrate_event = _event(name=get_hydrate_event(state), val=exp_val)
        exp_router_data = {
            "headers": {},
            "ip": client_ip,
            "sid": sid,
            "token": token,
            **hydrate_event.router_data,
        }
        update = await process(
            app,
            event=hydrate_event,
            sid=sid,
            headers={},
            client_ip=client_ip,
        ).__anext__()  # type: ignore

        # route change triggers: [full state dict, call on_load events, call set_is_hydrated(True)]
        assert update == StateUpdate(
            delta={
                state.get_name(): {
                    arg_name: exp_val,
                    f"comp_{arg_name}": exp_val,
                    constants.IS_HYDRATED: False,
                    "loaded": exp_index,
                    "counter": exp_index,
                    # "side_effect_counter": exp_index,
                }
            },
            events=[
                _dynamic_state_event(
                    name="on_load",
                    val=exp_val,
                    router_data=exp_router_data,
                ),
                _dynamic_state_event(
                    name="set_is_hydrated",
                    payload={"value": True},
                    val=exp_val,
                    router_data=exp_router_data,
                ),
            ],
        )
        assert state.dynamic == exp_val
        on_load_update = await process(
            app,
            event=_dynamic_state_event(name="on_load", val=exp_val),
            sid=sid,
            headers={},
            client_ip=client_ip,
        ).__anext__()  # type: ignore
        assert on_load_update == StateUpdate(
            delta={
                state.get_name(): {
                    # These computed vars _shouldn't_ be here, because they didn't change
                    arg_name: exp_val,
                    f"comp_{arg_name}": exp_val,
                    "loaded": exp_index + 1,
                },
            },
            events=[],
        )
        on_set_is_hydrated_update = await process(
            app,
            event=_dynamic_state_event(
                name="set_is_hydrated", payload={"value": True}, val=exp_val
            ),
            sid=sid,
            headers={},
            client_ip=client_ip,
        ).__anext__()  # type: ignore
        assert on_set_is_hydrated_update == StateUpdate(
            delta={
                state.get_name(): {
                    # These computed vars _shouldn't_ be here, because they didn't change
                    arg_name: exp_val,
                    f"comp_{arg_name}": exp_val,
                    "is_hydrated": True,
                },
            },
            events=[],
        )

        # a simple state update event should NOT trigger on_load or route var side effects
        update = await process(
            app,
            event=_dynamic_state_event(name="on_counter", val=exp_val),
            sid=sid,
            headers={},
            client_ip=client_ip,
        ).__anext__()  # type: ignore
        assert update == StateUpdate(
            delta={
                state.get_name(): {
                    # These computed vars _shouldn't_ be here, because they didn't change
                    f"comp_{arg_name}": exp_val,
                    arg_name: exp_val,
                    "counter": exp_index + 1,
                }
            },
            events=[],
        )
    assert state.loaded == len(exp_vals)
    assert state.counter == len(exp_vals)
    # print(f"Expected {exp_vals} rendering side effects, got {state.side_effect_counter}")
    # assert state.side_effect_counter == len(exp_vals)


@pytest.mark.asyncio
async def test_process_events(gen_state, mocker):
    """Test that an event is processed properly and that it is postprocessed
    n+1 times. Also check that the processing flag of the last stateupdate is set to
    False.

    Args:
        gen_state: The state.
        mocker: mocker object.
    """
    router_data = {
        "pathname": "/",
        "query": {},
        "token": "mock_token",
        "sid": "mock_sid",
        "headers": {},
        "ip": "127.0.0.1",
    }
    app = App(state=gen_state)
    mocker.patch.object(app, "postprocess", AsyncMock())
    event = Event(
        token="token", name="gen_state.go", payload={"c": 5}, router_data=router_data
    )

    async for _update in process(app, event, "mock_sid", {}, "127.0.0.1"):  # type: ignore
        pass

    assert app.state_manager.get_state("token").value == 5
    assert app.postprocess.call_count == 6


@pytest.mark.parametrize(
    ("state", "overlay_component", "exp_page_child"),
    [
        (DefaultState, default_overlay_component, None),
        (DefaultState, None, None),
        (DefaultState, Text.create("foo"), Text),
        (State, default_overlay_component, Fragment),
        (State, None, None),
        (State, Text.create("foo"), Text),
        (State, lambda: Text.create("foo"), Text),
    ],
)
def test_overlay_component(
    state: State | None,
    overlay_component: Component | ComponentCallable | None,
    exp_page_child: Type[Component] | None,
):
    """Test that the overlay component is set correctly.

    Args:
        state: The state class to pass to App.
        overlay_component: The overlay_component to pass to App.
        exp_page_child: The type of the expected child in the page fragment.
    """
    app = App(state=state, overlay_component=overlay_component)
    if exp_page_child is None:
        assert app.overlay_component is None
    elif isinstance(exp_page_child, Fragment):
        assert app.overlay_component is not None
        generated_component = app._generate_component(app.overlay_component)  # type: ignore
        assert isinstance(generated_component, Fragment)
        assert isinstance(
            generated_component.children[0],
            Cond,  # ConnectionModal is a Cond under the hood
        )
    else:
        assert app.overlay_component is not None
        assert isinstance(
            app._generate_component(app.overlay_component),  # type: ignore
            exp_page_child,
        )

    app.add_page(Box.create("Index"), route="/test")
    page = app.pages["test"]
    if exp_page_child is not None:
        assert len(page.children) == 3
        children_types = (type(child) for child in page.children)
        assert exp_page_child in children_types
    else:
        assert len(page.children) == 2
