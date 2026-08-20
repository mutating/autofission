import runpy


def test_dunder_main_module_can_be_loaded_without_running_cli() -> None:
    namespace = runpy.run_module('autofission.__main__', run_name='autofission.import_test')
    assert callable(namespace['main'])
