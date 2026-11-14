defmodule BeckFitness do
  @moduledoc """
  Helpers for the fitness functions beck generates from .beck/architecture.yaml.

  Written into the project by `beck fitness generate`; do not edit, it is
  rewritten at every generation. Dependencies are read from the `:imports`
  chunk of the compiled BEAM files, so they cover remote calls (macro
  expansions included); modules referenced only as atoms or struct literals
  are not seen.
  """

  import ExUnit.Assertions

  @doc "All the modules of `app`, without those compiled from `test/`."
  def modules(app) do
    Application.load(app)

    case Application.spec(app, :modules) do
      nil -> flunk("Application #{inspect(app)} is not available.")
      modules -> Enum.reject(modules, &test_support?/1)
    end
  end

  defp test_support?(module) do
    source = module.module_info(:compile) |> Keyword.get(:source, ~c"") |> to_string()
    String.contains?(source, "/test/")
  end

  @doc "True when `module` is `namespace` or lives under it."
  def within?(module, namespace) do
    module = Atom.to_string(module)
    namespace = Atom.to_string(namespace)
    module == namespace or String.starts_with?(module, namespace <> ".")
  end

  @doc "The distinct modules `module` calls, without itself."
  def dependencies(module) do
    {:ok, {^module, [imports: imports]}} = :beam_lib.chunks(:code.which(module), [:imports])

    imports
    |> Enum.map(fn {mod, _fun, _arity} -> mod end)
    |> Enum.uniq()
    |> Enum.reject(&(&1 == module))
  end

  @doc "Guards against vacuous passes: the namespace holds at least one module."
  def assert_defined(app, namespace) do
    assert Enum.any?(modules(app), &within?(&1, namespace)),
           "#{inspect(namespace)} has no modules in #{inspect(app)}."
  end

  @doc "contained_in: every module lives within one of `namespaces`, or is in `allow`."
  def assert_only_within(app, namespaces, allow) do
    strays =
      for module <- modules(app),
          module not in allow,
          not Enum.any?(namespaces, &within?(module, &1)),
          do: module

    assert strays == [], "Modules outside #{inspect(namespaces)}: #{inspect(strays)}"
  end

  @doc "no_dependency: no module within `from` calls a module within `targets`."
  def assert_no_dependency(app, from, targets) do
    violations =
      for module <- modules(app),
          within?(module, from),
          dependency <- dependencies(module),
          Enum.any?(targets, &within?(dependency, &1)),
          do: {module, dependency}

    assert violations == [], "#{inspect(from)} depends on #{inspect(targets)}: #{inspect(violations)}"
  end

  @doc """
  only_depends_on: within the system, `from` calls only itself and `targets`.
  Modules outside the root namespace `root` (Elixir, Erlang, the
  dependencies) and `root` itself are not judged.
  """
  def assert_only_depends_on(app, root, from, targets) do
    allowed = [from | targets]

    violations =
      for module <- modules(app),
          within?(module, from),
          dependency <- dependencies(module),
          within?(dependency, root) and dependency != root,
          not Enum.any?(allowed, &within?(dependency, &1)),
          do: {module, dependency}

    assert violations == [], "#{inspect(from)} depends outside #{inspect(targets)}: #{inspect(violations)}"
  end

  @doc "Runs one assertion of the generated table."
  def check(app, _root, {:defined, _from, [namespace], _allow}), do: assert_defined(app, namespace)
  def check(app, _root, {:contained_in, _from, namespaces, allow}), do: assert_only_within(app, namespaces, allow)
  def check(app, _root, {:no_dependency, from, targets, _allow}), do: assert_no_dependency(app, from, targets)
  def check(app, root, {:only_depends_on, from, targets, _allow}), do: assert_only_depends_on(app, root, from, targets)
end
