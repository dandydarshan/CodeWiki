import asyncio
import logging
import os
import json
import re
from collections import defaultdict
from typing import Dict, List, Any
from copy import deepcopy
import traceback

# Configure logging and monitoring
logger = logging.getLogger(__name__)

# Overview synthesis embeds every 1-depth child doc in its prompt. Left
# unbounded that grows with the repo until it overruns the model's context
# window, so cap the embedded docs at the project's existing per-call context
# yardstick (config.max_token_per_module) and trim the largest children first.
_OVERVIEW_REASONING_RE = re.compile(r"<(think|thinking|reasoning)>.*?</\1>", re.DOTALL | re.IGNORECASE)
_OVERVIEW_OPEN_REASONING_RE = re.compile(r"<(think|thinking|reasoning)>", re.IGNORECASE)

# Local imports
from codewiki.src.be.dependency_analyzer import DependencyGraphBuilder
from codewiki.src.be.backend import LLMBackend, get_backend
from codewiki.src.be.prompt_template import (
    REPO_OVERVIEW_PROMPT,
    MODULE_OVERVIEW_PROMPT,
)
from codewiki.src.be.cluster_modules import (
    cluster_modules,
    get_clustering_input_token_count,
)
from codewiki.src.config import (
    Config,
    FIRST_MODULE_TREE_FILENAME,
    MODULE_TREE_FILENAME,
    OVERVIEW_FILENAME
)
from codewiki.src.be.module_naming import (
    dedupe_module_tree_names,
    find_missing_module_docs,
    resolve_module_doc_path,
)
from codewiki.src.be.doc_state import node_at_path
from codewiki.src.be.utils import count_tokens, truncate_to_tokens
from codewiki.src.utils import file_manager


class IncompleteDocumentationError(Exception):
    """Raised when generation finishes but some modules have no doc file on disk."""

    def __init__(self, missing_modules: List[str]):
        self.missing_modules = missing_modules
        super().__init__(
            f"Documentation generation finished but {len(missing_modules)} module doc(s) "
            f"are missing: {', '.join(missing_modules)}"
        )


class DocumentationGenerator:
    """Main documentation generation orchestrator."""

    def __init__(self, config: Config, commit_id: str = None, backend: LLMBackend = None):
        self.config = config
        self.commit_id = commit_id
        self.graph_builder = DependencyGraphBuilder(config)
        self.backend: LLMBackend = backend or get_backend(config)
    
    def create_documentation_metadata(self, working_dir: str, components: Dict[str, Any], num_leaf_nodes: int):
        """Create a metadata file with documentation generation information."""
        from datetime import datetime
        
        metadata = {
            "generation_info": {
                "timestamp": datetime.now().isoformat(),
                "main_model": self.config.main_model,
                "generator_version": "1.0.1",
                "repo_path": self.config.repo_path,
                "commit_id": self.commit_id
            },
            "statistics": {
                "total_components": len(components),
                "leaf_nodes": num_leaf_nodes,
                "max_depth": self.config.max_depth
            },
            "files_generated": [
                "overview.md",
                "module_tree.json",
                "first_module_tree.json"
            ]
        }
        
        # Add generated markdown files to the metadata
        try:
            for file_path in os.listdir(working_dir):
                if file_path.endswith('.md') and file_path not in metadata["files_generated"]:
                    metadata["files_generated"].append(file_path)
        except Exception as e:
            logger.warning(f"Could not list generated files: {e}")
        
        metadata_path = os.path.join(working_dir, "metadata.json")
        file_manager.save_json(metadata, metadata_path)

    
    def get_processing_order(self, module_tree: Dict[str, Any], parent_path: List[str] = []) -> List[tuple[List[str], str]]:
        """Get the processing order using topological sort (leaf modules first)."""
        processing_order = []
        
        def collect_modules(tree: Dict[str, Any], path: List[str]):
            for module_name, module_info in tree.items():
                current_path = path + [module_name]
                
                # If this module has children, process them first
                if module_info.get("children") and isinstance(module_info["children"], dict) and module_info["children"]:
                    collect_modules(module_info["children"], current_path)
                    # Add this parent module after its children
                    processing_order.append((current_path, module_name))
                else:
                    # This is a leaf module, add it immediately
                    processing_order.append((current_path, module_name))
        
        collect_modules(module_tree, parent_path)
        return processing_order

    def get_processing_levels(
        self, module_tree: Dict[str, Any]
    ) -> List[List[tuple[List[str], str]]]:
        """Group the processing order into levels that can run concurrently.

        A module's only dependency is its own children — a parent overview reads
        its children's ``.md`` files. So every module at a given depth is
        independent of every other module at that depth, and documenting the
        deepest level first, then the next one up, respects every edge while
        letting each level run in parallel.
        """
        by_depth: Dict[int, List[tuple[List[str], str]]] = defaultdict(list)
        for module_path, module_name in self.get_processing_order(module_tree):
            by_depth[len(module_path)].append((module_path, module_name))
        return [by_depth[depth] for depth in sorted(by_depth, reverse=True)]

    def _module_concurrency(self) -> int:
        """Effective number of modules to document at once."""
        configured = max(1, int(getattr(self.config, "max_concurrent_modules", 1)))
        if configured == 1:
            return 1
        if not getattr(self.backend, "supports_parallel_modules", False):
            # The caw backend chdir()s the process per agent run and drives a
            # CLI subprocess, so concurrent runs would fight over the cwd.
            logger.info(
                "Backend %s does not support parallel module generation; "
                "documenting modules serially.",
                type(self.backend).__name__,
            )
            return 1
        return configured

    async def _process_one_module(
        self,
        module_path: List[str],
        module_name: str,
        module_info: Dict[str, Any],
        components: Dict[str, Any],
        working_dir: str,
        semaphore: "asyncio.Semaphore",
    ) -> None:
        """Document a single module. Failures are logged, not propagated."""
        module_key = "/".join(module_path)
        async with semaphore:
            try:
                if self.is_leaf_module(module_info):
                    logger.info(f"📄 Processing leaf module: {module_key}")
                    await self.backend.run_module_agent(
                        module_name=module_name,
                        components=components,
                        core_component_ids=module_info["components"],
                        module_path=module_path,
                        working_dir=working_dir,
                    )
                else:
                    logger.info(f"📁 Processing parent module: {module_key}")
                    await self.generate_parent_module_docs(module_path, working_dir)
            except Exception as e:
                logger.error(f"Failed to process module {module_key}: {str(e)}")
                logger.error(f"Traceback: {traceback.format_exc()}")

    def is_leaf_module(self, module_info: Dict[str, Any]) -> bool:
        """Check if a module is a leaf module (has no children or empty children)."""
        children = module_info.get("children", {})
        return not children or (isinstance(children, dict) and len(children) == 0)

    def build_overview_structure(self, module_tree: Dict[str, Any], module_path: List[str],
                                 working_dir: str) -> Dict[str, Any]:
        """Build structure for overview generation with 1-depth children docs and target indicator."""
        
        processed_module_tree = deepcopy(module_tree)
        module_info = processed_module_tree
        for path_part in module_path:
            module_info = module_info[path_part]
            if path_part != module_path[-1]:
                module_info = module_info.get("children", {})
            else:
                module_info["is_target_for_overview_generation"] = True

        if "children" in module_info:
            module_info = module_info["children"]

        child_docs: Dict[str, str] = {}
        for child_name, child_info in module_info.items():
            child_docs_path = self._resolve_child_docs_path(working_dir, child_name)
            if child_docs_path is not None:
                child_docs[child_name] = file_manager.load_text(child_docs_path)
            else:
                logger.warning(f"Module docs not found at {os.path.join(working_dir, f'{child_name}.md')}")
                child_docs[child_name] = ""

        for child_name, doc in self._fit_child_docs(child_docs).items():
            module_info[child_name]["docs"] = doc

        return processed_module_tree

    def _fit_child_docs(self, child_docs: Dict[str, str]) -> Dict[str, str]:
        """
        Trim embedded child docs so the overview prompt fits in one request.

        Every child that already fits its fair share is kept whole and its
        unused allowance is redistributed, so a handful of large modules are
        trimmed instead of penalising every module equally.
        """
        budget = self.config.max_token_per_module
        sizes = {name: count_tokens(doc) for name, doc in child_docs.items()}
        total = sum(sizes.values())
        if not sizes or total <= budget:
            return child_docs

        remaining = budget
        pending = set(sizes)
        allowances: Dict[str, int] = {}
        while pending:
            share = remaining // len(pending)
            fits = [name for name in pending if sizes[name] <= share]
            if not fits:
                # Everything left is oversized: split what's left evenly.
                allowances.update({name: share for name in pending})
                break
            for name in fits:
                allowances[name] = sizes[name]
                remaining -= sizes[name]
                pending.discard(name)

        trimmed = {
            name: truncate_to_tokens(doc, allowances.get(name, 0))
            for name, doc in child_docs.items()
        }
        shortened = sorted(name for name in sizes if sizes[name] > allowances.get(name, 0))
        logger.warning(
            "Child documentation for overview synthesis is %d tokens, above the "
            "%d-token budget; trimmed %d of %d module doc(s) to fit: %s. Raise "
            "--max-token-per-module if your model has room for more context.",
            total,
            budget,
            len(shortened),
            len(sizes),
            ", ".join(shortened),
        )
        return trimmed

    @staticmethod
    def _extract_overview_content(response: str, module_name: str) -> str:
        """
        Pull the markdown out of an overview reply, tolerating truncation.

        A reply cut off at the output limit never emits ``</OVERVIEW>``. Writing
        the raw response in that case would embed the opening tag and any
        reasoning preamble straight into the published doc, so strip both and
        keep whatever body arrived.
        """
        if not response or not response.strip():
            logger.error("Overview response for %s was empty; no documentation written.", module_name)
            return ""

        body = _OVERVIEW_REASONING_RE.sub("", response)
        open_reasoning = _OVERVIEW_OPEN_REASONING_RE.search(body)
        if open_reasoning:
            # Cut off mid-thought: nothing after the tag is answer text.
            body = body[: open_reasoning.start()]
            logger.warning(
                "Overview response for %s was cut off while the model was still "
                "reasoning; the generated doc may be incomplete.",
                module_name,
            )

        if "<OVERVIEW>" not in body:
            # Subscription CLIs (claude-code / codex) often ignore the wrapper
            # and return plain markdown - that case is fine as-is.
            logger.warning(
                "Overview response for %s missing <OVERVIEW> wrapper; using raw "
                "response as markdown.",
                module_name,
            )
            return body.strip()

        content = body.split("<OVERVIEW>", 1)[1]
        if "</OVERVIEW>" in content:
            return content.split("</OVERVIEW>", 1)[0].strip()

        logger.warning(
            "Overview response for %s is missing the closing </OVERVIEW> tag - the "
            "model ran out of output tokens. Keeping the partial overview; raise "
            "--max-tokens for a complete one.",
            module_name,
        )
        return content.strip()

    @staticmethod
    def _resolve_child_docs_path(working_dir: str, child_name: str) -> str | None:
        """Resolve the on-disk path for a child module's .md doc.

        Sub-agents sometimes save files under a sanitized variant of the
        module name (spaces → underscores, lowercased, etc.) rather than the
        exact key in the module tree. Try a small set of common variants
        before giving up so the overview prompt still gets the children's
        content as context.
        """
        return resolve_module_doc_path(working_dir, child_name)

    def validate_generated_docs(self, working_dir: str) -> List[str]:
        """Check the final module tree against the docs on disk.

        Returns the names of modules whose .md file is missing (plus
        "overview" if overview.md was never written).
        """
        module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)
        if not os.path.exists(module_tree_path):
            return []
        module_tree = file_manager.load_json(module_tree_path)
        return find_missing_module_docs(module_tree, working_dir)

    async def generate_module_documentation(self, components: Dict[str, Any], leaf_nodes: List[str]) -> str:
        """Generate documentation for all modules using dynamic programming approach."""
        # Prepare output directory
        working_dir = os.path.abspath(self.config.docs_dir)
        file_manager.ensure_directory(working_dir)

        module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)
        first_module_tree_path = os.path.join(working_dir, FIRST_MODULE_TREE_FILENAME)
        module_tree = file_manager.load_json(module_tree_path)
        first_module_tree = file_manager.load_json(first_module_tree_path)
        
        # Group into levels that can run concurrently (deepest level first)
        processing_levels = self.get_processing_levels(first_module_tree)

        # Process modules in dependency order
        final_module_tree = module_tree

        if len(module_tree) > 0:
            concurrency = self._module_concurrency()
            semaphore = asyncio.Semaphore(concurrency)
            total = sum(len(level) for level in processing_levels)
            logger.info(
                "Documenting %d module(s) across %d dependency level(s), "
                "%d at a time.",
                total,
                len(processing_levels),
                concurrency,
            )

            processed_modules = set()
            for depth_index, level in enumerate(processing_levels):
                # The structural plan comes from first_module_tree, which is
                # fixed; module_tree.json changes under us as sub-agents add
                # branches, so it can't be the source of truth for scheduling.
                tasks = []
                for module_path, module_name in level:
                    module_key = "/".join(module_path)
                    if module_key in processed_modules:
                        continue
                    module_info = node_at_path(first_module_tree, module_path)
                    if module_info is None:
                        logger.error(
                            "Module %s is missing from the planned tree; skipping.",
                            module_key,
                        )
                        continue
                    processed_modules.add(module_key)
                    tasks.append(
                        self._process_one_module(
                            module_path,
                            module_name,
                            module_info,
                            components,
                            working_dir,
                            semaphore,
                        )
                    )

                if not tasks:
                    continue
                logger.info(
                    "▶ Level %d/%d: %d module(s)",
                    depth_index + 1,
                    len(processing_levels),
                    len(tasks),
                )
                # Barrier between levels: a parent's overview reads its
                # children's .md files, so the level below must be complete.
                await asyncio.gather(*tasks)

            # Generate repo overview
            logger.info(f"📚 Generating repository overview")
            final_module_tree = await self.generate_parent_module_docs(
                [], working_dir
            )
        else:
            logger.info(f"Processing whole repo because repo can fit in the context window")
            repo_name = os.path.basename(os.path.normpath(self.config.repo_path))
            final_module_tree = await self.backend.run_module_agent(
                module_name=repo_name,
                components=components,
                core_component_ids=leaf_nodes,
                module_path=[],
                working_dir=working_dir,
            )

            # save final_module_tree to module_tree.json
            file_manager.save_json(final_module_tree, os.path.join(working_dir, MODULE_TREE_FILENAME))

            # rename repo_name.md to overview.md
            repo_overview_path = os.path.join(working_dir, f"{repo_name}.md")
            if os.path.exists(repo_overview_path):
                os.rename(repo_overview_path, os.path.join(working_dir, OVERVIEW_FILENAME))
        
        return working_dir

    async def generate_parent_module_docs(self, module_path: List[str], 
                                        working_dir: str) -> Dict[str, Any]:
        """Generate documentation for a parent module based on its children's documentation."""
        module_name = module_path[-1] if len(module_path) >= 1 else os.path.basename(os.path.normpath(self.config.repo_path))

        logger.info(f"Generating parent documentation for: {module_name}")
        
        # Load module tree
        module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)
        module_tree = file_manager.load_json(module_tree_path)

        # check if overview docs already exists
        overview_docs_path = os.path.join(working_dir, OVERVIEW_FILENAME)
        if os.path.exists(overview_docs_path):
            logger.info(f"✓ Overview docs already exists at {overview_docs_path}")
            return module_tree

        # check if parent docs already exists
        parent_docs_path = os.path.join(working_dir, f"{module_name if len(module_path) >= 1 else OVERVIEW_FILENAME.replace('.md', '')}.md")
        if os.path.exists(parent_docs_path):
            logger.info(f"✓ Parent docs already exists at {parent_docs_path}")
            return module_tree

        # Create repo structure with 1-depth children docs and target indicator
        repo_structure = self.build_overview_structure(module_tree, module_path, working_dir)

        prompt = MODULE_OVERVIEW_PROMPT.format(
            module_name=module_name,
            repo_structure=json.dumps(repo_structure, indent=4)
        ) if len(module_path) >= 1 else REPO_OVERVIEW_PROMPT.format(
            repo_name=module_name,
            repo_structure=json.dumps(repo_structure, indent=4)
        )
        
        logger.info(
            "Overview prompt for %s: %d tokens.", module_name, count_tokens(prompt)
        )

        try:
            parent_docs = self.backend.complete(prompt)
            parent_content = self._extract_overview_content(parent_docs, module_name)
            if not parent_content:
                # Writing an empty file here would satisfy the missing-docs check
                # and ship a blank overview; fail loudly instead.
                raise ValueError(
                    f"Overview generation for {module_name} produced no content"
                )
            file_manager.save_text(parent_content, parent_docs_path)
            
            logger.debug(f"Successfully generated parent documentation for: {module_name}")
            return module_tree
            
        except Exception as e:
            logger.error(f"Error generating parent documentation for {module_name}: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise
    
    async def run(self) -> None:
        """Run the complete documentation generation process using dynamic programming."""
        try:
            # Build dependency graph
            components, leaf_nodes = self.graph_builder.build_dependency_graph()

            logger.debug(f"Found {len(leaf_nodes)} leaf nodes")
            # logger.debug(f"Leaf nodes:\n{'\n'.join(sorted(leaf_nodes)[:200])}")
            # exit()
            
            # Cluster modules
            working_dir = os.path.abspath(self.config.docs_dir)
            file_manager.ensure_directory(working_dir)
            first_module_tree_path = os.path.join(working_dir, FIRST_MODULE_TREE_FILENAME)
            module_tree_path = os.path.join(working_dir, MODULE_TREE_FILENAME)
            
            # Check if module tree exists
            if os.path.exists(first_module_tree_path):
                logger.debug(f"Module tree found at {first_module_tree_path}")
                module_tree = file_manager.load_json(first_module_tree_path)
            else:
                logger.debug(f"Module tree not found at {module_tree_path}, clustering modules")
                clustering_tokens = get_clustering_input_token_count(
                    leaf_nodes, components
                )
                logger.info(
                    "Preparing %d leaf nodes for module clustering (%d tokens, threshold %d)",
                    len(leaf_nodes),
                    clustering_tokens,
                    self.config.max_token_per_module,
                )
                # Bind cluster_model into the completer so the backend uses the
                # configured clustering model (separate from main_model) when
                # one is set.  Caw mode's cluster_model is typically empty —
                # complete() falls back to its own _model in that case.
                cluster_model = self.config.cluster_model or None
                module_tree = cluster_modules(
                    leaf_nodes,
                    components,
                    self.config,
                    completer=lambda p: self.backend.complete(p, model=cluster_model),
                )
                # Only freshly clustered trees are deduped: renaming a cached
                # key whose .md already exists would orphan the doc.
                module_tree = dedupe_module_tree_names(module_tree)
                file_manager.save_json(module_tree, first_module_tree_path)
            
            file_manager.save_json(module_tree, module_tree_path)
            
            if len(module_tree) == 0:
                logger.info(
                    "Module clustering produced no top-level modules; continuing in "
                    "whole-repository documentation mode"
                )
            else:
                logger.info(
                    "Grouped components into %d top-level modules",
                    len(module_tree),
                )
            
            # Generate module documentation using dynamic programming approach
            # This processes leaf modules first, then parent modules
            working_dir = await self.generate_module_documentation(components, leaf_nodes)
            
            # Create documentation metadata
            self.create_documentation_metadata(working_dir, components, len(leaf_nodes))

            # Reconcile the final module tree against the docs on disk so
            # name collisions or failed sub-agents can't pass silently (issue #76)
            missing_docs = self.validate_generated_docs(working_dir)
            if missing_docs:
                for module_name in missing_docs:
                    logger.error(f"Module doc missing after generation: {module_name}.md")
                raise IncompleteDocumentationError(missing_docs)

            logger.debug(f"Documentation generation completed successfully using dynamic programming!")
            logger.debug(f"Processing order: leaf modules → parent modules → repository overview")
            logger.debug(f"Documentation saved to: {working_dir}")
            
        except Exception as e:
            logger.error(f"Documentation generation failed: {str(e)}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise
