"""
Generate command for documentation generation.
"""

import sys
import logging
import traceback
from pathlib import Path
from typing import Optional, List, Tuple
import click
import time

from codewiki.cli.config_manager import ConfigManager
from codewiki.cli.utils.errors import (
    ConfigurationError,
    RepositoryError,
    APIError,
    IncompleteGenerationError,
    handle_error,
    EXIT_SUCCESS,
)
from codewiki.cli.utils.repo_validator import (
    validate_repository,
    check_writable_output,
    is_git_repository,
    get_git_commit_hash,
    get_git_branch,
)
from codewiki.cli.utils.logging import create_logger
from codewiki.cli.adapters.doc_generator import CLIDocumentationGenerator
from codewiki.cli.utils.instructions import display_post_generation_instructions
from codewiki.cli.models.job import GenerationOptions
from codewiki.cli.models.config import AgentInstructions


def parse_patterns(patterns_str: str) -> List[str]:
    """Parse comma-separated patterns into a list."""
    if not patterns_str:
        return []
    return [p.strip() for p in patterns_str.split(',') if p.strip()]


def _detect_changed_files(
    repo_path: Path,
    output_dir: Path,
    logger,
    verbose: bool,
    compare_to: Optional[str] = None
) -> Optional[List[str]]:
    """
    Detect source files changed since the last documentation generation.

    Compares the commit recorded in metadata.json (or ``--compare-to``) with the
    current working tree — committed diff plus staged, unstaged and untracked
    files — falling back to file mtimes when git has no usable baseline. In a
    monorepo subdirectory only files under that subdirectory are returned, with
    the prefix stripped so paths line up with module_tree.json component IDs.

    Returns paths relative to *repo_path*, or None when there is no baseline to
    compare against (first run), meaning "generate everything".
    """
    from codewiki.src.be.incremental import detect_changed_files

    changes = detect_changed_files(repo_path, output_dir, compare_to=compare_to)
    if changes is None:
        if verbose:
            logger.debug("No previous generation to compare against — running full generation.")
        return None

    if verbose:
        baseline = changes.baseline or "previous generation"
        if changes.method == "git":
            baseline = baseline[:8]
        logger.debug(f"Changes since {baseline} (detected via {changes.method}):")
        for path in changes.files[:10]:
            logger.debug(f"  {path}")
        if len(changes.files) > 10:
            logger.debug(f"  ... and {len(changes.files) - 10} more")

    return changes.files


def _clear_generated_docs(output_dir: Path, logger, verbose: bool) -> None:
    """
    Remove CodeWiki's own output so --no-cache really regenerates everything.

    Generation resumes by skipping modules whose ``.md`` already exists and by
    reusing the cached module tree, so "ignore cache" has to mean deleting those
    artifacts. Only files CodeWiki generates are touched.
    """
    targets = sorted(output_dir.glob("*.md"))
    for name in ("module_tree.json", "first_module_tree.json"):
        candidate = output_dir / name
        if candidate.exists():
            targets.append(candidate)

    for path in targets:
        try:
            path.unlink()
        except OSError as e:
            logger.warning(f"Could not remove {path.name}: {e}")
            continue
        if verbose:
            logger.debug(f"Removed cached artifact: {path.name}")

    if targets:
        logger.info(f"  --no-cache: cleared {len(targets)} cached artifact(s) from {output_dir}")


@click.command(name="generate")
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default="docs",
    help="Output directory for generated documentation (default: ./docs)",
)
@click.option(
    "--create-branch",
    is_flag=True,
    help="Create a new git branch for documentation changes",
)
@click.option(
    "--github-pages",
    is_flag=True,
    help="Generate index.html for GitHub Pages deployment",
)
@click.option(
    "--no-cache",
    is_flag=True,
    help="Force full regeneration, ignoring cache",
)
@click.option(
    "--include",
    "-i",
    type=str,
    default=None,
    help="Comma-separated file patterns to include (e.g., '*.cs,*.py'). Overrides defaults.",
)
@click.option(
    "--exclude",
    "-e",
    type=str,
    default=None,
    help="Comma-separated patterns to exclude (e.g., '*Tests*,*Specs*,test_*')",
)
@click.option(
    "--focus",
    "-f",
    type=str,
    default=None,
    help="Comma-separated modules/paths to focus on (e.g., 'src/core,src/api')",
)
@click.option(
    "--doc-type",
    "-t",
    type=click.Choice(['api', 'architecture', 'user-guide', 'developer'], case_sensitive=False),
    default=None,
    help="Type of documentation to generate",
)
@click.option(
    "--instructions",
    type=str,
    default=None,
    help="Custom instructions for the documentation agent",
)
@click.option(
    "--use-gitignore/--no-gitignore",
    default=None,
    help="Apply Git ignore rules during analysis (default: enabled)",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Show detailed progress and debug information",
)
@click.option(
    "--max-tokens",
    type=int,
    default=None,
    help="Maximum tokens for LLM response (overrides config)",
)
@click.option(
    "--max-token-per-module",
    type=int,
    default=None,
    help="Maximum tokens per module for clustering (overrides config)",
)
@click.option(
    "--max-token-per-leaf-module",
    type=int,
    default=None,
    help="Maximum tokens per leaf module (overrides config)",
)
@click.option(
    "--max-concurrent-modules",
    type=int,
    default=None,
    help="Sibling modules to document in parallel (1 = serial, overrides config)",
)
@click.option(
    "--max-depth",
    type=int,
    default=None,
    help="Maximum depth for hierarchical decomposition (overrides config)",
)
@click.option(
    "--update",
    is_flag=True,
    help="Incremental update: rebuild only the modules whose sources changed since the last generation (committed or not)",
)
@click.option(
    "--compare-to",
    type=str,
    default=None,
    help="Commit hash to compare against for incremental updates (overrides stored commit in metadata.json)",
)
@click.pass_context
def generate_command(
    ctx,
    output: str,
    create_branch: bool,
    github_pages: bool,
    no_cache: bool,
    include: Optional[str],
    exclude: Optional[str],
    focus: Optional[str],
    doc_type: Optional[str],
    instructions: Optional[str],
    use_gitignore: Optional[bool],
    verbose: bool,
    max_tokens: Optional[int],
    max_token_per_module: Optional[int],
    max_token_per_leaf_module: Optional[int],
    max_concurrent_modules: Optional[int],
    max_depth: Optional[int],
    update: bool = False,
    compare_to: Optional[str] = None
):
    """
    Generate comprehensive documentation for a code repository.
    
    Analyzes the current repository and generates documentation using LLM-powered
    analysis. Documentation is output to ./docs/ by default.
    
    Examples:
    
    \b
    # Basic generation
    $ codewiki generate
    
    \b
    # With git branch creation and GitHub Pages
    $ codewiki generate --create-branch --github-pages
    
    \b
    # Only re-document what changed since the last run
    $ codewiki generate --update

    \b
    # Same, but diff against a specific commit
    $ codewiki generate --compare-to abc1234

    \b
    # Force full regeneration (clears cached tree and existing docs)
    $ codewiki generate --no-cache

    \b
    # Analyze ignored files as well
    $ codewiki generate --no-gitignore
    
    \b
    # C# project: only .cs files, exclude tests
    $ codewiki generate --include "*.cs" --exclude "*Tests*,*Specs*"
    
    \b
    # Focus on specific modules with architecture docs
    $ codewiki generate --focus "src/core,src/api" --doc-type architecture
    
    \b
    # Custom instructions
    $ codewiki generate --instructions "Focus on public APIs and include usage examples"
    
    \b
    # Override max tokens for this generation
    $ codewiki generate --max-tokens 16384
    
    \b
    # Set all max token limits
    $ codewiki generate --max-tokens 32768 --max-token-per-module 40000 --max-token-per-leaf-module 20000
    
    \b
    # Override max depth for hierarchical decomposition
    $ codewiki generate --max-depth 3
    """
    logger = create_logger(verbose=verbose)
    start_time = time.time()
    
    # Suppress httpx INFO logs
    logging.getLogger("httpx").setLevel(logging.WARNING)
    
    try:
        # Pre-generation checks
        logger.step("Validating configuration...", 1, 4)
        
        # Load configuration
        config_manager = ConfigManager()
        if not config_manager.load():
            raise ConfigurationError(
                "Configuration not found or invalid.\n\n"
                "Please run 'codewiki config set' to configure your LLM API credentials:\n"
                "  codewiki config set --api-key <your-api-key> --base-url <api-url> \\\n"
                "    --main-model <model> --cluster-model <model>\n\n"
                "For more help: codewiki config --help"
            )
        
        if not config_manager.is_configured():
            raise ConfigurationError(
                "Configuration is incomplete. Please run 'codewiki config validate'"
            )
        
        config = config_manager.get_config()
        api_key = config_manager.get_api_key()
        
        logger.success("Configuration valid")
        
        # Validate repository
        logger.step("Validating repository...", 2, 4)
        
        repo_path = Path.cwd()
        repo_path, languages = validate_repository(repo_path)
        
        logger.success(f"Repository valid: {repo_path.name}")
        if verbose:
            logger.debug(f"Detected languages: {', '.join(f'{lang} ({count} files)' for lang, count in languages)}")
        
        # Check git repository
        if not is_git_repository(repo_path):
            if create_branch:
                raise RepositoryError(
                    "Not a git repository.\n\n"
                    "The --create-branch flag requires a git repository.\n\n"
                    "To initialize a git repository: git init"
                )
            else:
                logger.warning("Not a git repository. Git features unavailable.")
        
        # Validate output directory
        output_dir = Path(output).expanduser().resolve()
        check_writable_output(output_dir.parent)
        
        logger.success(f"Output directory: {output_dir}")
        
        # If a base commit is specified to compare against, implicitly enable update
        if compare_to:
            update = True

        # --no-cache means a full rebuild, so incremental reuse cannot apply.
        if update and no_cache:
            logger.warning("--no-cache forces a full regeneration; ignoring --update.")
            update = False

        # Incremental update: detect changed files. Which modules that
        # invalidates is decided after clustering, once the current module tree
        # is known — a file can only be mapped to a module that still exists.
        changed_files = None
        incremental = False
        if update and output_dir.exists():
            changed_files = _detect_changed_files(repo_path, output_dir, logger, verbose, compare_to=compare_to)
            if changed_files is None:
                logger.warning("No previous generation found to update — generating from scratch.")
            else:
                incremental = True
                if changed_files:
                    logger.info(f"  Detected {len(changed_files)} changed file(s) since the last generation.")
                else:
                    logger.info("  No source changes detected; only missing or stale docs will be rebuilt.")

        # Check for existing documentation
        if not update and output_dir.exists() and list(output_dir.glob("*.md")):
            if not click.confirm(
                f"\n{output_dir} already contains documentation. Overwrite?",
                default=True
            ):
                logger.info("Generation cancelled by user.")
                sys.exit(EXIT_SUCCESS)

        if no_cache and output_dir.exists():
            _clear_generated_docs(output_dir, logger, verbose)
        
        # Git branch creation (if requested)
        branch_name = None
        if create_branch:
            logger.step("Creating git branch...", 3, 4)
            
            from codewiki.cli.git_manager import GitManager
            
            git_manager = GitManager(repo_path)
            
            # Check clean working directory
            is_clean, status_msg = git_manager.check_clean_working_directory()
            if not is_clean:
                raise RepositoryError(
                    "Working directory has uncommitted changes.\n\n"
                    f"{status_msg}\n\n"
                    "Cannot create documentation branch with uncommitted changes.\n"
                    "Please commit or stash your changes first:\n"
                    "  git add -A && git commit -m \"Your message\"\n"
                    "  # or\n"
                    "  git stash"
                )
            
            # Create branch
            branch_name = git_manager.create_documentation_branch()
            logger.success(f"Created branch: {branch_name}")
        
        # Generate documentation
        logger.step("Generating documentation...", 4, 4)
        click.echo()
        
        # Record how this run was invoked; attached to the job below.
        generation_options = GenerationOptions(
            create_branch=create_branch,
            github_pages=github_pages,
            no_cache=no_cache,
            custom_output=output if output != "docs" else None
        )

        # Create runtime agent instructions from CLI options
        runtime_instructions = None
        if any([include, exclude, focus, doc_type, instructions]):
            runtime_instructions = AgentInstructions(
                include_patterns=parse_patterns(include) if include else None,
                exclude_patterns=parse_patterns(exclude) if exclude else None,
                focus_modules=parse_patterns(focus) if focus else None,
                doc_type=doc_type,
                custom_instructions=instructions,
            )
            
            if verbose:
                if include:
                    logger.debug(f"Include patterns: {parse_patterns(include)}")
                if exclude:
                    logger.debug(f"Exclude patterns: {parse_patterns(exclude)}")
                if focus:
                    logger.debug(f"Focus modules: {parse_patterns(focus)}")
                if doc_type:
                    logger.debug(f"Doc type: {doc_type}")
                if instructions:
                    logger.debug(f"Custom instructions: {instructions}")
        
        # Log max token settings if verbose
        if verbose:
            effective_max_tokens = max_tokens if max_tokens is not None else config.max_tokens
            effective_max_token_per_module = max_token_per_module if max_token_per_module is not None else config.max_token_per_module
            effective_max_token_per_leaf = max_token_per_leaf_module if max_token_per_leaf_module is not None else config.max_token_per_leaf_module
            effective_max_depth = max_depth if max_depth is not None else config.max_depth
            effective_use_gitignore = use_gitignore if use_gitignore is not None else config.use_gitignore
            logger.debug(f"Max tokens: {effective_max_tokens}")
            logger.debug(f"Max token/module: {effective_max_token_per_module}")
            logger.debug(f"Max token/leaf module: {effective_max_token_per_leaf}")
            logger.debug(f"Max concurrent modules: {max_concurrent_modules if max_concurrent_modules is not None else config.max_concurrent_modules}")
            logger.debug(f"Max depth: {effective_max_depth}")
            logger.debug(f"Use gitignore: {effective_use_gitignore}")
        
        # Get agent instructions (merge runtime with persistent)
        agent_instructions_dict = None
        if runtime_instructions and not runtime_instructions.is_empty():
            # Merge with persistent settings
            merged = AgentInstructions(
                include_patterns=runtime_instructions.include_patterns or (config.agent_instructions.include_patterns if config.agent_instructions else None),
                exclude_patterns=runtime_instructions.exclude_patterns or (config.agent_instructions.exclude_patterns if config.agent_instructions else None),
                focus_modules=runtime_instructions.focus_modules or (config.agent_instructions.focus_modules if config.agent_instructions else None),
                doc_type=runtime_instructions.doc_type or (config.agent_instructions.doc_type if config.agent_instructions else None),
                custom_instructions=runtime_instructions.custom_instructions or (config.agent_instructions.custom_instructions if config.agent_instructions else None),
            )
            agent_instructions_dict = merged.to_dict()
        elif config.agent_instructions and not config.agent_instructions.is_empty():
            agent_instructions_dict = config.agent_instructions.to_dict()
        
        # Create generator
        # Get commit_id early so it can be stored in metadata.json for --update support
        commit_id = get_git_commit_hash(repo_path)
        generator = CLIDocumentationGenerator(
            repo_path=repo_path,
            output_dir=output_dir,
            config={
                'main_model': config.main_model,
                'cluster_model': config.cluster_model,
                'fallback_model': config.fallback_model,
                'base_url': config.base_url,
                'api_key': api_key,
                'provider': getattr(config, 'provider', 'openai-compatible'),
                'aws_region': getattr(config, 'aws_region', 'us-east-1'),
                'agent_instructions': agent_instructions_dict,
                # Max token settings (runtime overrides take precedence)
                'max_tokens': max_tokens if max_tokens is not None else config.max_tokens,
                'max_token_per_module': max_token_per_module if max_token_per_module is not None else config.max_token_per_module,
                'max_token_per_leaf_module': max_token_per_leaf_module if max_token_per_leaf_module is not None else config.max_token_per_leaf_module,
                'max_concurrent_modules': max_concurrent_modules if max_concurrent_modules is not None else config.max_concurrent_modules,
                # Max depth setting (runtime override takes precedence)
                'max_depth': max_depth if max_depth is not None else config.max_depth,
                # Gitignore setting (runtime override takes precedence)
                'use_gitignore': use_gitignore if use_gitignore is not None else config.use_gitignore,
            },
            verbose=verbose,
            generate_html=github_pages,
            commit_id=commit_id,
            incremental=incremental,
            changed_files=changed_files,
            no_cache=no_cache,
        )
        generator.job.generation_options = generation_options

        # Run generation
        job = generator.generate()
        
        # Post-generation
        generation_time = time.time() - start_time
        
        # Get repository info
        repo_url = None
        current_branch = get_git_branch(repo_path)
        
        if is_git_repository(repo_path):
            try:
                import git
                repo = git.Repo(repo_path)
                if repo.remotes:
                    repo_url = repo.remotes.origin.url
            except:
                pass
        
        # Display instructions
        display_post_generation_instructions(
            output_dir=output_dir,
            repo_name=repo_path.name,
            repo_url=repo_url,
            branch_name=branch_name,
            github_pages=github_pages,
            files_generated=job.files_generated,
            statistics={
                'module_count': job.module_count,
                'total_files_analyzed': job.statistics.total_files_analyzed,
                'generation_time': generation_time,
                'total_tokens_used': job.statistics.total_tokens_used,
            }
        )
        
    except ConfigurationError as e:
        logger.error(e.message)
        logger.error(f"Traceback: {traceback.format_exc()}")
        sys.exit(e.exit_code)
    except RepositoryError as e:
        logger.error(e.message)
        logger.error(f"Traceback: {traceback.format_exc()}")
        sys.exit(e.exit_code)
    except APIError as e:
        logger.error(e.message)
        logger.error(f"Traceback: {traceback.format_exc()}")
        sys.exit(e.exit_code)
    except IncompleteGenerationError as e:
        click.secho(f"\n✗ {e.message}", fg="red", err=True)
        for module_name in e.missing_modules:
            click.secho(f"  - {module_name}.md", fg="red", err=True)
        click.echo(
            "Re-run the same command to resume generation for the missing modules.",
            err=True,
        )
        sys.exit(e.exit_code)
    except KeyboardInterrupt:
        click.echo("\n\nInterrupted by user")
        sys.exit(130)
    except Exception as e:
        sys.exit(handle_error(e, verbose=verbose))
