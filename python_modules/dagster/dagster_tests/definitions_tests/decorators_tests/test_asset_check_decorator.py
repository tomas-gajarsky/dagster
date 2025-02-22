import re
from typing import NamedTuple, Optional

import pytest
from dagster import (
    AssetCheckResult,
    AssetCheckSeverity,
    AssetCheckSpec,
    AssetKey,
    AssetSelection,
    ConfigurableResource,
    DagsterEventType,
    DagsterInstance,
    Definitions,
    EventRecordsFilter,
    ExecuteInProcessResult,
    IOManager,
    MetadataValue,
    ObserveResult,
    ResourceParam,
    SourceAsset,
    asset,
    asset_check,
    define_asset_job,
    multi_asset_check,
    observable_source_asset,
)
from dagster._core.definitions.asset_check_spec import AssetCheckKey
from dagster._core.errors import (
    DagsterInvalidDefinitionError,
    DagsterInvalidSubsetError,
    DagsterInvariantViolationError,
)
from dagster._core.execution.context.compute import AssetCheckExecutionContext


def execute_assets_and_checks(
    assets=None,
    asset_checks=None,
    raise_on_error: bool = True,
    resources=None,
    instance=None,
    selection: Optional[AssetSelection] = None,
) -> ExecuteInProcessResult:
    defs = Definitions(
        assets=assets,
        asset_checks=asset_checks,
        resources=resources,
        jobs=[
            define_asset_job(
                "job1",
                selection=selection or (AssetSelection.all() | AssetSelection.all_asset_checks()),
            )
        ],
    )
    job_def = defs.get_job_def("job1")
    return job_def.execute_in_process(raise_on_error=raise_on_error, instance=instance)


def test_asset_check_decorator():
    @asset_check(asset="asset1", description="desc", metadata={"foo": "bar"})
    def check1():
        return AssetCheckResult(passed=True)

    spec = check1.get_spec_for_check_key(AssetCheckKey(AssetKey(["asset1"]), "check1"))
    assert spec.name == "check1"
    assert spec.description == "desc"
    assert spec.asset_key == AssetKey("asset1")
    assert spec.metadata == {"foo": "bar"}


def test_asset_check_decorator_name():
    @asset_check(asset="asset1", description="desc", name="check1")
    def _check():
        return AssetCheckResult(passed=True)

    spec = _check.get_spec_for_check_key(AssetCheckKey(AssetKey(["asset1"]), "check1"))
    assert spec.name == "check1"


def test_asset_check_with_prefix():
    @asset(key_prefix="prefix")
    def asset1(): ...

    @asset_check(asset=asset1)
    def my_check():
        return AssetCheckResult(passed=True)

    spec = my_check.get_spec_for_check_key(
        AssetCheckKey(AssetKey(["prefix", "asset1"]), "my_check")
    )
    assert spec.asset_key == AssetKey(["prefix", "asset1"])


def test_asset_check_input_with_prefix():
    @asset(key_prefix="prefix")
    def asset1(): ...

    @asset_check(asset=asset1)
    def my_check(asset1):
        return AssetCheckResult(passed=True)

    spec = my_check.get_spec_for_check_key(
        AssetCheckKey(AssetKey(["prefix", "asset1"]), "my_check")
    )
    assert spec.asset_key == AssetKey(["prefix", "asset1"])


def test_execute_asset_and_check():
    @asset
    def asset1(): ...

    @asset_check(asset=asset1, description="desc")
    def check1(context: AssetCheckExecutionContext):
        assert context.op_execution_context.asset_key_for_input("asset1") == asset1.key
        return AssetCheckResult(
            asset_key=asset1.key,
            check_name="check1",
            passed=True,
            metadata={"foo": "bar"},
        )

    instance = DagsterInstance.ephemeral()
    result = execute_assets_and_checks(assets=[asset1], asset_checks=[check1], instance=instance)
    assert result.success

    check_evals = result.get_asset_check_evaluations()
    assert len(check_evals) == 1
    check_eval = check_evals[0]
    assert check_eval.asset_key == asset1.key
    assert check_eval.check_name == "check1"
    assert check_eval.metadata == {"foo": MetadataValue.text("bar")}

    assert check_eval.target_materialization_data is not None
    assert check_eval.target_materialization_data.run_id == result.run_id
    materialization_record = instance.get_event_records(
        EventRecordsFilter(event_type=DagsterEventType.ASSET_MATERIALIZATION)
    )[0]
    assert check_eval.target_materialization_data.storage_id == materialization_record.storage_id
    assert check_eval.target_materialization_data.timestamp == materialization_record.timestamp

    assert (
        len(
            instance.get_event_records(
                EventRecordsFilter(event_type=DagsterEventType.ASSET_CHECK_EVALUATION_PLANNED)
            )
        )
        == 1
    )
    assert (
        len(
            instance.get_event_records(
                EventRecordsFilter(event_type=DagsterEventType.ASSET_CHECK_EVALUATION)
            )
        )
        == 1
    )
    assert (
        len(
            instance.event_log_storage.get_asset_check_execution_history(
                AssetCheckKey(asset_key=AssetKey("asset1"), name="check1"), limit=10
            )
        )
        == 1
    )


def test_execute_check_without_asset():
    @asset_check(asset="asset1", description="desc")
    def check1():
        return AssetCheckResult(passed=True, metadata={"foo": "bar"})

    result = execute_assets_and_checks(asset_checks=[check1])
    assert result.success

    check_evals = result.get_asset_check_evaluations()
    assert len(check_evals) == 1
    check_eval = check_evals[0]
    assert check_eval.asset_key == AssetKey("asset1")
    assert check_eval.check_name == "check1"
    assert check_eval.metadata == {"foo": MetadataValue.text("bar")}

    assert check_eval.target_materialization_data is None


def test_execute_check_and_asset_in_separate_run():
    @asset
    def asset1(): ...

    @asset_check(asset=asset1, description="desc")
    def check1(context: AssetCheckExecutionContext):
        assert context.op_execution_context.asset_key_for_input("asset1") == asset1.key
        return AssetCheckResult(
            asset_key=asset1.key,
            check_name="check1",
            passed=True,
            metadata={"foo": "bar"},
        )

    instance = DagsterInstance.ephemeral()
    materialize_result = execute_assets_and_checks(assets=[asset1], instance=instance)

    result = execute_assets_and_checks(asset_checks=[check1], instance=instance)
    assert result.success

    check_evals = result.get_asset_check_evaluations()
    assert len(check_evals) == 1
    check_eval = check_evals[0]

    assert check_eval.target_materialization_data is not None
    assert check_eval.target_materialization_data.run_id == materialize_result.run_id
    materialization_record = instance.get_event_records(
        EventRecordsFilter(event_type=DagsterEventType.ASSET_MATERIALIZATION)
    )[0]
    assert check_eval.target_materialization_data.storage_id == materialization_record.storage_id
    assert check_eval.target_materialization_data.timestamp == materialization_record.timestamp


def test_execute_check_and_unrelated_asset():
    @asset
    def asset2(): ...

    @asset_check(asset="asset1", description="desc")
    def check1():
        return AssetCheckResult(passed=True)

    result = execute_assets_and_checks(assets=[asset2], asset_checks=[check1])
    assert result.success

    materialization_events = result.get_asset_materialization_events()
    assert len(materialization_events) == 1

    check_evals = result.get_asset_check_evaluations()
    assert len(check_evals) == 1
    check_eval = check_evals[0]
    assert check_eval.asset_key == AssetKey("asset1")
    assert check_eval.check_name == "check1"


def test_check_doesnt_execute_if_asset_fails():
    check_executed = [False]

    @asset
    def asset1():
        raise ValueError()

    @asset_check(asset=asset1)
    def asset1_check(context: AssetCheckExecutionContext):
        check_executed[0] = True

    result = execute_assets_and_checks(
        assets=[asset1], asset_checks=[asset1_check], raise_on_error=False
    )
    assert not result.success

    assert not check_executed[0]


def test_check_decorator_unexpected_asset_key():
    @asset_check(asset="asset1", description="desc")
    def asset1_check():
        return AssetCheckResult(asset_key=AssetKey("asset2"), passed=True)

    with pytest.raises(
        DagsterInvariantViolationError,
        match=re.escape(
            "Received unexpected AssetCheckResult. It targets asset 'asset2' which is not targeted"
            " by any of the checks currently being evaluated. Targeted assets: ['asset1']."
        ),
    ):
        execute_assets_and_checks(asset_checks=[asset1_check])


def test_asset_check_separate_op_downstream_still_executes():
    @asset
    def asset1(): ...

    @asset_check(asset=asset1)
    def asset1_check(context):
        return AssetCheckResult(passed=False)

    @asset(deps=[asset1])
    def asset2(): ...

    result = execute_assets_and_checks(assets=[asset1, asset2], asset_checks=[asset1_check])
    assert result.success

    materialization_events = result.get_asset_materialization_events()
    assert len(materialization_events) == 2

    check_evals = result.get_asset_check_evaluations()
    assert len(check_evals) == 1
    check_eval = check_evals[0]
    assert check_eval.asset_key == AssetKey("asset1")
    assert check_eval.check_name == "asset1_check"
    assert not check_eval.passed


def test_blocking_check_skip_downstream():
    @asset
    def asset1(): ...

    @asset_check(asset=asset1, blocking=True)
    def check1(context):
        return AssetCheckResult(passed=False)

    @asset(deps=[asset1])
    def asset2(): ...

    result = execute_assets_and_checks(
        assets=[asset1, asset2], asset_checks=[check1], raise_on_error=False
    )
    assert not result.success

    materialization_events = result.get_asset_materialization_events()
    assert len(materialization_events) == 1

    check_evals = result.get_asset_check_evaluations()
    assert len(check_evals) == 1
    check_eval = check_evals[0]
    assert check_eval.asset_key == AssetKey("asset1")
    assert check_eval.check_name == "check1"
    assert not check_eval.passed

    error = result.failure_data_for_node("asset1_check1").error
    assert error.message.startswith(
        "dagster._core.errors.DagsterAssetCheckFailedError: Blocking check 'check1' for asset 'asset1'"
        " failed with ERROR severity."
    )


def test_blocking_check_with_source_asset_fail():
    asset1 = SourceAsset("asset1")

    @asset_check(asset=asset1, blocking=True)
    def check1(context):
        return AssetCheckResult(passed=False)

    @asset(deps=[asset1])
    def asset2(): ...

    result = execute_assets_and_checks(
        assets=[asset1, asset2], asset_checks=[check1], raise_on_error=False
    )
    assert not result.success

    materialization_events = result.get_asset_materialization_events()
    assert len(materialization_events) == 0

    check_evals = result.get_asset_check_evaluations()
    assert len(check_evals) == 1
    check_eval = check_evals[0]
    assert check_eval.asset_key == AssetKey("asset1")
    assert check_eval.check_name == "check1"
    assert not check_eval.passed

    error = result.failure_data_for_node("asset1_check1").error
    assert error.message.startswith(
        "dagster._core.errors.DagsterAssetCheckFailedError: Blocking check 'check1' for asset 'asset1'"
        " failed with ERROR severity."
    )


def test_error_severity_with_source_asset_success():
    asset1 = SourceAsset("asset1", io_manager_key="asset1_io_manager")

    @asset_check(asset=asset1)
    def check1(context):
        return AssetCheckResult(passed=True, severity=AssetCheckSeverity.ERROR)

    @asset
    def asset2(asset1):
        assert asset1 == 5

    class MyIOManager(IOManager):
        def load_input(self, context):
            return 5

        def handle_output(self, context, obj):
            raise NotImplementedError()

    result = execute_assets_and_checks(
        assets=[asset1, asset2],
        asset_checks=[check1],
        raise_on_error=False,
        resources={"asset1_io_manager": MyIOManager()},
    )
    assert result.success

    materialization_events = result.get_asset_materialization_events()
    assert len(materialization_events) == 1

    check_evals = result.get_asset_check_evaluations()
    assert len(check_evals) == 1
    check_eval = check_evals[0]
    assert check_eval.asset_key == AssetKey("asset1")
    assert check_eval.check_name == "check1"
    assert check_eval.passed


def test_definitions_conflicting_checks():
    def make_check():
        @asset_check(asset="asset1")
        def check1(context): ...

        return check1

    with pytest.raises(
        DagsterInvalidDefinitionError,
        match="Duplicate asset check key.+asset1.+check1",
    ):
        Definitions(asset_checks=[make_check(), make_check()])


def test_definitions_same_name_different_asset():
    def make_check_for_asset(asset_key: str):
        @asset_check(asset=asset_key)
        def check1(context):
            return AssetCheckResult(passed=True)

        return check1

    Definitions(asset_checks=[make_check_for_asset("asset1"), make_check_for_asset("asset2")])


def test_definitions_same_asset_different_name():
    def make_check(check_name: str):
        @asset_check(asset="asset1", name=check_name)
        def _check(context):
            return AssetCheckResult(passed=True)

        return _check

    Definitions(asset_checks=[make_check("check1"), make_check("check2")])


def test_resource_params():
    class MyResource(NamedTuple):
        value: int

    @asset_check(asset=AssetKey("asset1"))
    def check1(my_resource: ResourceParam[MyResource]):
        assert my_resource.value == 5
        return AssetCheckResult(passed=True)

    execute_assets_and_checks(asset_checks=[check1], resources={"my_resource": MyResource(5)})


def test_resource_params_with_resource_defs():
    class MyResource(NamedTuple):
        value: int

    @asset_check(asset=AssetKey("asset1"), resource_defs={"my_resource": MyResource(5)})
    def check1(my_resource: ResourceParam[MyResource]):
        assert my_resource.value == 5
        return AssetCheckResult(passed=True)

    execute_assets_and_checks(asset_checks=[check1])


def test_required_resource_keys():
    @asset
    def my_asset():
        pass

    @asset_check(asset=my_asset, required_resource_keys={"my_resource"})
    def my_check(context):
        assert context.resources.my_resource == "foobar"
        return AssetCheckResult(passed=True)

    execute_assets_and_checks(
        assets=[my_asset], asset_checks=[my_check], resources={"my_resource": "foobar"}
    )


def test_resource_definitions():
    @asset
    def my_asset():
        pass

    class MyResource(ConfigurableResource):
        name: str

    @asset_check(asset=my_asset, resource_defs={"my_resource": MyResource(name="foobar")})
    def my_check(context: AssetCheckExecutionContext):
        assert context.resources.my_resource.name == "foobar"
        return AssetCheckResult(passed=True)

    execute_assets_and_checks(assets=[my_asset], asset_checks=[my_check])


def test_resource_definitions_satisfy_required_keys():
    @asset
    def my_asset():
        pass

    class MyResource(ConfigurableResource):
        name: str

    @asset_check(
        asset=my_asset,
        resource_defs={"my_resource": MyResource(name="foobar")},
        required_resource_keys={"my_resource"},
    )
    def my_check(context: AssetCheckExecutionContext):
        assert context.resources.my_resource.name == "foobar"
        return AssetCheckResult(passed=True)

    execute_assets_and_checks(assets=[my_asset], asset_checks=[my_check])


def test_job_only_execute_checks_downstream_of_selected_assets():
    @asset
    def asset1(): ...

    @asset
    def asset2(): ...

    @asset_check(asset=asset1)
    def check1():
        return AssetCheckResult(passed=False)

    @asset_check(asset=asset2)
    def check2():
        return AssetCheckResult(passed=False)

    defs = Definitions(
        assets=[asset1, asset2],
        asset_checks=[check1, check2],
        jobs=[define_asset_job("job1", selection=[asset1])],
    )
    job_def = defs.get_job_def("job1")
    result = job_def.execute_in_process()
    assert result.success

    check_evals = result.get_asset_check_evaluations()
    assert len(check_evals) == 1
    check_eval = check_evals[0]
    assert check_eval.asset_key == asset1.key
    assert check_eval.check_name == "check1"


def test_asset_not_provided():
    with pytest.raises(Exception):

        @asset_check(description="desc")
        def check1(): ...


def test_managed_input():
    @asset
    def asset1() -> int:
        return 4

    @asset_check(asset=asset1, description="desc")
    def check1(asset1):
        assert asset1 == 4
        return AssetCheckResult(passed=True)

    class MyIOManager(IOManager):
        def load_input(self, context):
            assert context.asset_key == asset1.key
            return 4

        def handle_output(self, context, obj): ...

    spec = check1.get_spec_for_check_key(AssetCheckKey(AssetKey(["asset1"]), "check1"))
    assert spec.name == "check1"
    assert spec.asset_key == asset1.key

    assert execute_assets_and_checks(
        assets=[asset1], asset_checks=[check1], resources={"io_manager": MyIOManager()}
    ).success


def test_multiple_managed_inputs():
    with pytest.raises(
        DagsterInvalidDefinitionError,
        match=re.escape(
            "When defining check 'check1', multiple assets provided as parameters:"
            " ['asset1', 'asset2']. These should either match the target asset or be specified "
            "in 'additional_ins'."
        ),
    ):

        @asset_check(asset="asset1", description="desc")
        def check1(asset1, asset2): ...


def test_managed_input_with_context():
    @asset
    def asset1() -> int:
        return 4

    @asset_check(asset=asset1, description="desc")
    def check1(context: AssetCheckExecutionContext, asset1):
        assert context
        assert asset1 == 4
        return AssetCheckResult(passed=True)

    spec = check1.get_spec_for_check_key(AssetCheckKey(AssetKey(["asset1"]), "check1"))
    assert spec.name == "check1"
    assert spec.asset_key == asset1.key

    execute_assets_and_checks(assets=[asset1], asset_checks=[check1])


def test_doesnt_invoke_io_manager():
    class DummyIOManager(IOManager):
        def handle_output(self, context, obj):
            assert False

        def load_input(self, context):
            assert False

    @asset_check(asset="asset1", description="desc")
    def check1(context: AssetCheckExecutionContext):
        return AssetCheckResult(passed=True)

    execute_assets_and_checks(asset_checks=[check1], resources={"io_manager": DummyIOManager()})


def assert_check_eval(
    check_eval,
    asset_key: AssetKey,
    check_name: str,
    passed: bool,
    run_id: str,
    instance: DagsterInstance,
):
    assert check_eval.asset_key == asset_key
    assert check_eval.check_name == check_name
    assert check_eval.passed == passed

    assert check_eval.target_materialization_data is not None
    assert check_eval.target_materialization_data.run_id == run_id
    materialization_record = instance.fetch_materializations(
        records_filter=asset_key, limit=1
    ).records[0]
    assert check_eval.target_materialization_data.storage_id == materialization_record.storage_id
    assert check_eval.target_materialization_data.timestamp == materialization_record.timestamp


def test_multi_asset_check():
    @asset
    def asset1(): ...

    @asset
    def asset2(): ...

    @multi_asset_check(
        specs=[
            AssetCheckSpec("check1", asset=asset1),
            AssetCheckSpec("check2", asset=asset1),
            AssetCheckSpec("check3", asset=asset2),
        ]
    )
    def checks(context):
        materialization_records = context.instance.get_event_records(
            EventRecordsFilter(event_type=DagsterEventType.ASSET_MATERIALIZATION)
        )
        assert len(materialization_records) == 2
        assert {record.asset_key for record in materialization_records} == {asset1.key, asset2.key}

        yield AssetCheckResult(passed=True, asset_key="asset1", check_name="check1")
        yield AssetCheckResult(passed=False, asset_key="asset1", check_name="check2")
        yield AssetCheckResult(passed=True, asset_key="asset2", check_name="check3")

    instance = DagsterInstance.ephemeral()
    result = execute_assets_and_checks(
        assets=[asset1, asset2],
        asset_checks=[checks],
        instance=instance,
    )

    assert result.success

    check_evals = result.get_asset_check_evaluations()
    assert len(check_evals) == 3

    assert_check_eval(check_evals[0], asset1.key, "check1", True, result.run_id, instance)
    assert_check_eval(check_evals[1], asset1.key, "check2", False, result.run_id, instance)
    assert_check_eval(check_evals[2], asset2.key, "check3", True, result.run_id, instance)

    assert (
        len(
            instance.get_event_records(
                EventRecordsFilter(event_type=DagsterEventType.ASSET_CHECK_EVALUATION_PLANNED)
            )
        )
        == 3
    )
    assert (
        len(
            instance.get_event_records(
                EventRecordsFilter(event_type=DagsterEventType.ASSET_CHECK_EVALUATION)
            )
        )
        == 3
    )
    assert (
        len(
            instance.event_log_storage.get_asset_check_execution_history(
                AssetCheckKey(asset_key=AssetKey("asset1"), name="check1"), limit=10
            )
        )
        == 1
    )

    with pytest.raises(DagsterInvalidSubsetError):
        execute_assets_and_checks(
            asset_checks=[checks],
            instance=instance,
            selection=AssetSelection.checks(AssetCheckKey(asset2.key, "check3")),
        )


def test_multi_asset_check_subset():
    @asset
    def asset1(): ...

    @asset
    def asset2(): ...

    asset1_check1 = AssetCheckSpec("check1", asset=asset1)
    asset2_check3 = AssetCheckSpec("check3", asset=asset2)

    @multi_asset_check(
        specs=[asset1_check1, AssetCheckSpec("check2", asset=asset1), asset2_check3],
        can_subset=True,
    )
    def checks(context):
        assert context.selected_asset_check_keys == {asset1_check1.key, asset2_check3.key}

        yield AssetCheckResult(passed=True, asset_key="asset1", check_name="check1")
        yield AssetCheckResult(passed=False, asset_key="asset2", check_name="check3")

    instance = DagsterInstance.ephemeral()
    result = execute_assets_and_checks(
        assets=[asset1, asset2],
        asset_checks=[checks],
        instance=instance,
        selection=AssetSelection.assets(asset1, asset2).without_checks()
        | AssetSelection.checks(
            AssetCheckKey(asset1.key, "check1"), AssetCheckKey(asset2.key, "check3")
        ),
    )

    assert result.success

    check_evals = result.get_asset_check_evaluations()
    assert len(check_evals) == 2

    assert_check_eval(check_evals[0], asset1.key, "check1", True, result.run_id, instance)
    assert_check_eval(check_evals[1], asset2.key, "check3", False, result.run_id, instance)

    assert (
        len(
            instance.get_event_records(
                EventRecordsFilter(event_type=DagsterEventType.ASSET_CHECK_EVALUATION_PLANNED)
            )
        )
        == 2
    )
    assert (
        len(
            instance.get_event_records(
                EventRecordsFilter(event_type=DagsterEventType.ASSET_CHECK_EVALUATION)
            )
        )
        == 2
    )
    assert (
        len(
            instance.event_log_storage.get_asset_check_execution_history(
                AssetCheckKey(asset_key=AssetKey("asset1"), name="check1"), limit=10
            )
        )
        == 1
    )


def test_target_materialization_observable_source_asset():
    @observable_source_asset
    def asset1():
        return ObserveResult()

    @asset_check(asset=asset1)
    def check1():
        return AssetCheckResult(passed=True)

    result = execute_assets_and_checks(assets=[asset1], asset_checks=[check1])
    assert result.success

    check_evals = result.get_asset_check_evaluations()
    assert len(check_evals) == 1
    check_eval = check_evals[0]
    assert check_eval.asset_key == AssetKey("asset1")
    assert check_eval.check_name == "check1"

    assert check_eval.target_materialization_data is None
