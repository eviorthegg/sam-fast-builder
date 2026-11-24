from setuptools import setup

setup(
    name="sam-fast-builder",
    version="2.1.0",
    description="Incremental AWS SAM builds: copy changed code, rebuild only when dependencies change",
    py_modules=["sam_builder"],
    install_requires=["pyyaml>=6.0"],
    entry_points={"console_scripts": ["sam-builder=sam_builder:main"]},
    python_requires=">=3.8",
)
