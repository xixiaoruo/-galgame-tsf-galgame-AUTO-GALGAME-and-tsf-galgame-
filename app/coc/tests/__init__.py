"""CoC 引擎单测（**零第三方依赖**，标准库即可运行）。

每个文件都有 `main()`，可用两种方式跑：

    python -m app.coc.tests.test_tuning_provenance     # 单文件
    python -m app.coc.tests.run_all                    # 全部

设计约定：**构造不出反例的断言不许进验收** —— 由 `harness.TestSuite.expect`
把 `counterexample` 设为必填参数来强制。
"""
