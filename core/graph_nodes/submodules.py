import asyncio
import logging
from typing import Dict, Any, List, Tuple

from core.graph_nodes.initial_flow import execute_single_search
from models.models import SearchQuery, EnhancedModule, Submodule, SubmoduleContent, LearningPathState
from parsers.parsers import submodule_parser, module_queries_parser
from services.services import get_llm, get_search_tool
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

# Import the extracted prompts
from prompts.learning_path_prompts import (
    SUBMODULE_PLANNING_PROMPT,
    # Removed imports for prompts now defined inline
    # SUBMODULE_QUERY_GENERATION_PROMPT,
    # SUBMODULE_CONTENT_DEVELOPMENT_PROMPT
)
# Optional: Import the prompt registry for more advanced prompt management
# from prompts.prompt_registry import registry

from core.graph_nodes.helpers import run_chain, batch_items, escape_curly_braces

async def plan_submodules(state: LearningPathState) -> Dict[str, Any]:
    """
    Breaks down each module into 3-5 detailed submodules using an LLM chain.
    Processes modules in parallel based on the parallel_count configuration.
    
    Args:
        state: The current LearningPathState containing basic modules.
        
    Returns:
        A dictionary with enhanced modules (each including planned submodules) and a list of steps.
    """
    logging.info("Planning submodules for each module in parallel")
    if not state.get("modules"):
        logging.warning("No modules available")
        return {"enhanced_modules": [], "steps": ["No modules available"]}
    
    # Get parallelism configuration - use the parallel_count from the user settings
    parallel_count = state.get("parallel_count", 2)
    logging.info(f"Planning submodules with parallelism of {parallel_count}")
    
    progress_callback = state.get("progress_callback")
    if progress_callback:
        await progress_callback(f"Planning submodules for {len(state['modules'])} modules with parallelism of {parallel_count}...")
    
    # Create semaphore to control concurrency based on parallel_count
    sem = asyncio.Semaphore(parallel_count)
    
    # Helper function to plan submodules for a module with semaphore
    async def plan_module_submodules_bounded(idx, module):
        async with sem:  # Limits concurrency based on parallel_count
            return await plan_module_submodules(state, idx, module)
    
    # Create tasks to process each module in parallel
    tasks = [plan_module_submodules_bounded(idx, module) 
             for idx, module in enumerate(state.get("modules", []))]
    
    # Execute tasks in parallel and collect results
    enhanced_modules = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Process results and handle any exceptions
    processed_modules = []
    for idx, result in enumerate(enhanced_modules):
        if isinstance(result, Exception):
            logging.error(f"Error processing module {idx+1}: {str(result)}")
            # Create a fallback module with no submodules
            from models.models import EnhancedModule
            processed_modules.append(EnhancedModule(
                title=state["modules"][idx].title,
                description=state["modules"][idx].description,
                submodules=[]
            ))
        else:
            processed_modules.append(result)
    
    if progress_callback:
        total_submodules = sum(len(m.submodules) for m in processed_modules)
        await progress_callback(f"Planned {total_submodules} submodules across {len(processed_modules)} modules in parallel")
    
    return {
        "enhanced_modules": processed_modules, 
        "steps": [f"Planned submodules for {len(processed_modules)} modules in parallel using {parallel_count} parallel processes"]
    }

async def plan_module_submodules(state: LearningPathState, idx: int, module) -> Any:
    """
    Plans submodules for a specific module.
    
    Args:
        state: The current state.
        idx: Index of the module.
        module: The module to process.
        
    Returns:
        Enhanced module with planned submodules.
    """
    logging.info(f"Planning submodules for module {idx+1}: {module.title}")
    
    # Get language from state
    output_language = state.get('language', 'en')
    
    learning_path_context = "\n".join([f"Module {i+1}: {mod.title}\n{mod.description}" 
                                       for i, mod in enumerate(state["modules"])])
    
    # Check if a specific number of submodules was requested
    submodule_count_instruction = ""
    if state.get("desired_submodule_count"):
        submodule_count_instruction = f"IMPORTANT: Create EXACTLY {state['desired_submodule_count']} submodules for this module. Not more, not less."
    
    # Modify the prompt to include the submodule count instruction if specified
    base_prompt = SUBMODULE_PLANNING_PROMPT
    if submodule_count_instruction:
        # Insert the instruction before the format_instructions placeholder
        base_prompt = base_prompt.replace("{format_instructions}", f"{submodule_count_instruction}\n\n{{format_instructions}}")
    
    # Using the extracted prompt template
    prompt = ChatPromptTemplate.from_template(base_prompt)
    try:
        result = await run_chain(prompt, lambda: get_llm(key_provider=state.get("google_key_provider")), submodule_parser, {
            "user_topic": state["user_topic"],
            "module_title": module.title,
            "module_description": module.description,
            "learning_path_context": learning_path_context,
            "language": output_language,
            "format_instructions": submodule_parser.get_format_instructions()
        })
        submodules = result.submodules
        
        # If a specific number of submodules was requested but not achieved, adjust the list
        if state.get("desired_submodule_count") and len(submodules) != state["desired_submodule_count"]:
            logging.warning(f"Requested {state['desired_submodule_count']} submodules but got {len(submodules)} for module {idx+1}")
            if len(submodules) > state["desired_submodule_count"]:
                # Trim excess submodules if we got too many
                submodules = submodules[:state["desired_submodule_count"]]
                logging.info(f"Trimmed submodules to match requested count of {state['desired_submodule_count']}")
        
        # Set order for each submodule
        for i, sub in enumerate(submodules):
            sub.order = i + 1
            
        # Create the enhanced module
        try:
            enhanced_module = module.model_copy(update={"submodules": submodules})
        except Exception:
            from models.models import EnhancedModule
            enhanced_module = EnhancedModule(
                title=module.title,
                description=module.description,
                submodules=submodules
            )
            
        logging.info(f"Planned {len(submodules)} submodules for module {idx+1}")
        return enhanced_module
        
    except Exception as e:
        logging.error(f"Error planning submodules for module {idx+1}: {str(e)}")
        raise  # Propagate the exception to be handled in the parent function

async def initialize_submodule_processing(state: LearningPathState) -> Dict[str, Any]:
    """
    Initializes the batch processing of submodules by setting up tracking variables.
    Implements LangGraph's map-reduce pattern for optimized parallel processing.
    
    Args:
        state: The current state with enhanced modules.
        
    Returns:
        Updated state with batch processing tracking variables.
    """
    logging.info("Initializing submodule batch processing with LangGraph-optimized distribution")
    
    progress_callback = state.get("progress_callback")
    if progress_callback:
        await progress_callback("Preparing to enhance modules with detailed content using LangGraph parallel processing...")
    
    enhanced_modules = state.get("enhanced_modules")
    if not enhanced_modules:
        logging.warning("No enhanced modules available")
        return {
            "submodule_batches": [],
            "current_submodule_batch_index": 0,
            "submodules_in_process": {},
            "developed_submodules": [],
            "steps": ["No enhanced modules available"]
        }
    
    # Get the user-configured parallelism setting
    submodule_parallel_count = state.get("submodule_parallel_count", 2)
    
    # Create a flat list of all submodules across all modules
    # This follows LangGraph's pattern of mapping items first
    all_submodules = []
    for module_id, module in enumerate(enhanced_modules):
        if module.submodules:
            for sub_id in range(len(module.submodules)):
                all_submodules.append({
                    "module_id": module_id,
                    "sub_id": sub_id,
                    "module_title": module.title,
                    "submodule_title": module.submodules[sub_id].title,
                    "module_idx": module_id,     # For balanced sorting later
                    "total_submodules": len(module.submodules),  # Needed for balanced batching
                })
    
    if not all_submodules:
        logging.warning("No valid submodules found")
        return {
            "submodule_batches": [],
            "current_submodule_batch_index": 0,
            "submodules_in_process": {},
            "developed_submodules": [],
            "steps": ["No valid submodules found"]
        }
    
    # Optimize the distribution to balance work across batches
    # Instead of just alternating modules, we implement a more sophisticated strategy:
    # 1. Modules with more submodules should have their submodules distributed widely
    # 2. We'll sort by a custom key to achieve maximum distribution
    
    # First step: count submodules per module
    module_counts = {}
    for item in all_submodules:
        module_id = item["module_id"]
        module_counts[module_id] = module_counts.get(module_id, 0) + 1
    
    # Second step: create a more sophisticated distribution key
    # This prioritizes spreading out submodules from larger modules first
    def distribution_key(item):
        # Primary sort: submodule index divided by total submodules in module
        # This spreads out submodules from same module evenly
        relative_position = item["sub_id"] / max(1, item["total_submodules"])
        
        # Secondary sort: module ID to ensure consistent ordering
        module_id = item["module_id"]
        
        # The key prioritizes distributing all "first" submodules, then all "second" etc.
        # While keeping different modules together within each group
        return (relative_position, module_id)
    
    # Sort by our balanced distribution key
    all_submodules.sort(key=distribution_key)
    
    # Create the actual pairs for batch processing
    all_pairs = [(item["module_id"], item["sub_id"]) for item in all_submodules]
    
    # Create batches of size submodule_parallel_count
    submodule_batches = batch_items(all_pairs, submodule_parallel_count)
    
    total_submodules = len(all_submodules)
    total_batches = len(submodule_batches)
    
    logging.info(
        f"Using LangGraph map-reduce pattern: Organized {total_submodules} submodules into "
        f"{total_batches} batches with parallelism of {submodule_parallel_count}")
    
    if progress_callback:
        await progress_callback(
            f"Preparing to process {total_submodules} submodules in {total_batches} batches "
            f"with {submodule_parallel_count} submodules in parallel using LangGraph pattern"
        )
    
    return {
        "submodule_batches": submodule_batches,
        "current_submodule_batch_index": 0,
        "submodules_in_process": {},
        "developed_submodules": [],
        "steps": [f"Initialized LangGraph-optimized submodule processing with batch size {submodule_parallel_count}"]
    }

async def process_submodule_batch(state: LearningPathState) -> Dict[str, Any]:
    """
    Processes a batch of submodules in parallel, following LangGraph's map-reduce pattern.
    
    This function:
    1. Maps a set of submodules to parallel processing tasks
    2. Executes those tasks concurrently up to the user's configured parallelism limit
    3. Reduces the results back into the main state
    
    Args:
        state: The current state with enhanced modules and batch tracking.
        
    Returns:
        Updated state with processed submodules.
    """
    # Get the batch processing configuration
    submodule_parallel_count = state.get("submodule_parallel_count", 2)
    progress_callback = state.get("progress_callback")
    sub_batches = state.get("submodule_batches") or []
    current_index = state.get("current_submodule_batch_index", 0)
    
    logging.info(f"Processing submodule batch {current_index+1}/{len(sub_batches)} with parallelism of {submodule_parallel_count}")
    
    if progress_callback:
        await progress_callback(f"Processing batch {current_index+1} of {len(sub_batches)} with {submodule_parallel_count} parallel tasks...")
    
    # Check if all batches are already processed
    if current_index >= len(sub_batches):
        logging.info("All submodule batches processed")
        return {"steps": ["All submodule batches processed"]}
    
    # Get the current batch to process
    current_batch = sub_batches[current_index]
    enhanced_modules = state.get("enhanced_modules", [])
    submodules_in_process = state.get("submodules_in_process", {})
    
    # Create tasks for parallel processing
    tasks = []
    task_to_submodule_map = []  # Track which task corresponds to which submodule
    
    # Setup tasks for all submodules in the current batch
    for module_id, sub_id in current_batch:
        key = (module_id, sub_id)
        
        # Skip already processed submodules
        if key in submodules_in_process and submodules_in_process[key].get("status") == "completed":
            continue
            
        # Get the module and submodule
        if module_id < len(enhanced_modules) and sub_id < len(enhanced_modules[module_id].submodules):
            module = enhanced_modules[module_id]
            submodule = module.submodules[sub_id]
            
            # Create a task for processing this submodule
            task = process_single_submodule(state, module_id, sub_id, module, submodule)
            tasks.append(task)
            task_to_submodule_map.append(key)
            
            # Log for debugging
            logging.debug(f"Created task for submodule {module_id}.{sub_id}: {submodule.title}")
        else:
            logging.warning(f"Invalid submodule indices: module {module_id}, submodule {sub_id}")
    
    # Process all submodules in this batch using gather for true parallelism
    # following LangGraph's map-reduce pattern
    if tasks:
        if progress_callback:
            await progress_callback(f"Processing {len(tasks)} submodules in parallel using LangGraph pattern...")
        
        try:
            # Start time for measuring performance
            import time
            start_time = time.time()
            
            # Execute all tasks using gather for true parallelism (the "map" phase)
            results = await asyncio.gather(*tasks, return_exceptions=False)
            
            # Calculate processing time
            elapsed_time = time.time() - start_time
            per_submodule_time = elapsed_time / len(tasks) if tasks else 0
            logging.info(f"Completed batch in {elapsed_time:.2f} seconds ({per_submodule_time:.2f}s per submodule with {len(tasks)} in parallel)")
            
            # Process results (the "reduce" phase)
            # This combines the results from parallel execution back into the main state
            success_count = 0
            error_count = 0
            
            # Update the submodules_in_process dictionary with the results
            for result in results:
                if result.get("status") == "completed":
                    module_id = result.get("module_id")
                    sub_id = result.get("sub_id")
                    key = (module_id, sub_id)
                    submodules_in_process[key] = result
                    success_count += 1
                elif result.get("status") == "error":
                    module_id = result.get("module_id")
                    sub_id = result.get("sub_id")
                    key = (module_id, sub_id)
                    submodules_in_process[key] = result
                    error_message = f"Error in submodule {module_id}.{sub_id}: {result.get('error', 'Unknown error')}"
                    logging.error(error_message)
                    error_count += 1
            
            if progress_callback:
                await progress_callback(
                    f"Completed batch {current_index+1}/{len(sub_batches)}: "
                    f"{success_count} successful, {error_count} failed in {elapsed_time:.2f} seconds"
                )
                
            logging.info(f"Batch {current_index+1} results: {success_count} successful, {error_count} failed")
        except Exception as e:
            logging.error(f"Error in processing submodule batch: {str(e)}")
            if progress_callback:
                await progress_callback(f"Error processing batch: {str(e)}")
    else:
        logging.info(f"No tasks to process in batch {current_index+1}")
        if progress_callback:
            await progress_callback(f"No tasks to process in batch {current_index+1}")
    
    # Move to the next batch
    next_index = current_index + 1
    
    # Update the list of developed submodules based on completed tasks
    # This is part of the "reduce" phase from the map-reduce pattern
    developed_submodules = state.get("developed_submodules", [])
    
    # Process all submodules from the current batch
    for module_id, sub_id in current_batch:
        key = (module_id, sub_id)
        data = submodules_in_process.get(key, {})
        
        if data.get("status") == "completed" and module_id < len(enhanced_modules):
            module = enhanced_modules[module_id]
            if sub_id < len(module.submodules):
                # Create a SubmoduleContent object for the completed submodule
                developed_submodules.append(SubmoduleContent(
                    module_id=module_id,
                    submodule_id=sub_id,
                    title=module.submodules[sub_id].title,
                    description=module.submodules[sub_id].description,
                    search_queries=data.get("search_queries", []),
                    search_results=data.get("search_results", []),
                    content=data.get("content", "")
                ))
    
    # Update progress based on completion percentage
    if progress_callback:
        processed_count = current_index + 1
        total_count = len(sub_batches)
        percentage = min(100, int((processed_count / total_count) * 100))
        await progress_callback(f"Completed batch {current_index+1}/{len(sub_batches)} ({percentage}% of submodules processed)")
    
    # Return updated state
    return {
        "submodules_in_process": submodules_in_process,
        "current_submodule_batch_index": next_index,
        "developed_submodules": developed_submodules,
        "steps": [f"Processed submodule batch {current_index+1} with {len(tasks)} parallel tasks using LangGraph pattern"]
    }

async def process_single_submodule(
    state: LearningPathState, 
    module_id: int, 
    sub_id: int, 
    module: EnhancedModule, 
    submodule: Submodule
) -> Dict[str, Any]:
    """
    Processes a single submodule in an optimized atomic operation that combines
    query generation, search, and content development.
    
    This function follows LangGraph's map-reduce pattern for parallel processing:
    1. It receives a single submodule state to process
    2. It processes the entire submodule independently
    3. It returns a complete result that can be integrated back into the main state
    
    Args:
        state: The current state.
        module_id: The index of the module.
        sub_id: The index of the submodule.
        module: The module object.
        submodule: The submodule object.
        
    Returns:
        Updated submodule with content.
    """
    logger = logging.getLogger("learning_path.submodule_processor")
    logger.info(f"Processing submodule {sub_id+1} in module {module_id+1}: {submodule.title}")
    
    progress_callback = state.get("progress_callback")
    
    try:
        # Track timing for performance analysis
        import time
        start_time = time.time()
        
        # Update progress if callback is available
        if progress_callback:
            await progress_callback(f"Processing: Module {module_id+1} > Submodule {sub_id+1}: {submodule.title}")
        
        # STEP 1: Generate submodule-specific queries
        step_start = time.time()
        submodule_search_queries = await generate_submodule_specific_queries(
            state, module_id, sub_id, module, submodule
        )
        query_gen_time = time.time() - step_start
        logger.debug(f"Generated {len(submodule_search_queries)} queries for submodule in {query_gen_time:.2f}s")
        
        if progress_callback:
            await progress_callback(
                f"Generated search query for {module.title} > {submodule.title}"
            )
        
        # STEP 2: Execute search for each query in parallel
        step_start = time.time()
        submodule_search_results = await execute_submodule_specific_searches(
            state, module_id, sub_id, module, submodule, submodule_search_queries
        )
        search_time = time.time() - step_start
        logger.debug(f"Completed {len(submodule_search_results)} searches in {search_time:.2f}s")
        
        if progress_callback:
            await progress_callback(
                f"Completed research for {module.title} > {submodule.title}"
            )
        
        # STEP 3: Develop submodule content based on search results
        step_start = time.time()
        submodule_content = await develop_submodule_specific_content(
            state, module_id, sub_id, module, submodule, submodule_search_queries, submodule_search_results
        )
        content_time = time.time() - step_start
        
        # Calculate total processing time
        total_time = time.time() - start_time
        
        logger.info(
            f"Completed submodule {module_id+1}.{sub_id+1} in {total_time:.2f}s "
            f"(Query: {query_gen_time:.2f}s, Search: {search_time:.2f}s, Content: {content_time:.2f}s)"
        )
        
        if progress_callback:
            await progress_callback(
                f"Completed development for {module.title} > {submodule.title} in {total_time:.2f}s"
            )
        
        # Return the completed submodule data with module/submodule identifiers for proper "reduce" phase
        return {
            "status": "completed", 
            "module_id": module_id,
            "sub_id": sub_id,
            "search_queries": submodule_search_queries, 
            "search_results": submodule_search_results, 
            "content": submodule_content,
            "processing_time": {
                "total": total_time,
                "query_generation": query_gen_time,
                "search": search_time,
                "content_development": content_time
            }
        }
    except Exception as e:
        logger.exception(f"Error processing submodule {sub_id+1} of module {module_id+1}: {str(e)}")
        if progress_callback:
            await progress_callback(f"Error in {module.title} > {submodule.title}: {str(e)}")
        
        # Return error status with identifiers to maintain batch processing integrity
        return {
            "status": "error", 
            "module_id": module_id,
            "sub_id": sub_id,
            "error": str(e)
        }

async def generate_submodule_specific_queries(
    state: LearningPathState, 
    module_id: int, 
    sub_id: int, 
    module: EnhancedModule, 
    submodule: Submodule
) -> List[SearchQuery]:
    """
    Generates a single high-quality search query specific to a submodule.
    
    Args:
        state: The current LearningPathState.
        module_id: Index of the parent module.
        sub_id: Index of the submodule.
        module: The EnhancedModule instance.
        submodule: The Submodule instance.
        
    Returns:
        A list containing a single SearchQuery instance.
    """
    logger = logging.getLogger("learning_path.query_generator")
    logger.info(f"Generating search query for submodule {module_id}.{sub_id}: {submodule.title}")
    
    # Get the Google key provider from state
    google_key_provider = state.get("google_key_provider")
    
    # Get language information from state
    output_language = state.get('language', 'en')
    search_language = state.get('search_language', 'en')
    
    # Import función para escapar llaves
    from core.graph_nodes.helpers import escape_curly_braces
    
    # Preparar contexto sobre el módulo y el submódulo
    # Escapar las llaves en todos los campos de texto
    user_topic = escape_curly_braces(state["user_topic"])
    module_title = escape_curly_braces(module.title)
    module_description = escape_curly_braces(module.description)
    submodule_title = escape_curly_braces(submodule.title)
    submodule_description = escape_curly_braces(submodule.description)
    submodule_depth = escape_curly_braces(submodule.depth_level)
    
    learning_context = {
        "topic": user_topic,
        "module_title": module_title,
        "module_description": module_description,
        "submodule_title": submodule_title,
        "submodule_description": submodule_description,
        "depth_level": submodule_depth
    }
    
    # Create context about other modules and submodules
    other_modules = []
    for i, m in enumerate(state.get("enhanced_modules", [])):
        other_modules.append({
            "title": escape_curly_braces(m.title),
            "description": escape_curly_braces(m.description[:200] + "..." if len(m.description) > 200 else m.description),
            "is_current": i == module_id
        })
    
    # Compile the context with a description of other modules
    learning_path_context = f"""
Topic: {learning_context['topic']}

Current Module: {learning_context['module_title']} 
Description: {learning_context['module_description']}

Current Submodule: {learning_context['submodule_title']}
Description: {learning_context['submodule_description']}
Depth Level: {learning_context['depth_level']}

Other Modules in Learning Path:
"""
    for m in other_modules:
        learning_path_context += f"- {'[Current] ' if m['is_current'] else ''}{m['title']}: {m['description']}\n"
    
    # Set up module context and counts
    module_context = f"Current Module: {module_title}\nDescription: {module_description}"
    module_count = len(state.get("enhanced_modules", []))
    submodule_count = len(module.submodules)
    
    # Modified prompt template for single optimal query
    single_query_prompt = """
# EXPERT RESEARCHER INSTRUCTIONS

You are tasked with creating a SINGLE OPTIMAL search query for in-depth research on a specific educational submodule.

## SUBMODULE CONTEXT
- Topic: {user_topic}
- Module: {module_title} (Module {module_order} of {module_count})
- Submodule: {submodule_title} (Submodule {submodule_order} of {submodule_count})
- Description: {submodule_description}

## MODULE CONTEXT
{module_context}

## LEARNING PATH CONTEXT
{learning_path_context}

## LANGUAGE STRATEGY
- Final content will be presented to the user in {output_language}.
- For search queries, use {search_language} to maximize information quality and quantity.
- If the topic is highly specialized or regional/cultural, consider whether the search language should be adjusted for optimal results.

## YOUR TASK

Create ONE exceptionally well-crafted search query that will:
1. Target the most critical information needed for this specific submodule
2. Be comprehensive enough to gather essential educational content
3. Retrieve detailed, accurate, and authoritative information
4. Focus precisely on the unique aspects of this submodule
5. Balance breadth and depth to maximize learning value

Your query should be:
- Specific and targeted toward educational/tutorial content
- Formulated to return high-quality information
- Structured to capture the most current and relevant resources
- Phrased to avoid redundancy with other parts of the learning path

Provide:
1. The optimal search query
2. A brief but comprehensive rationale explaining why this is the ideal query for this submodule

{format_instructions}
"""
    
    prompt = ChatPromptTemplate.from_template(single_query_prompt)
    
    from pydantic import BaseModel, Field
    
    class SingleSearchQueryOutput(BaseModel):
        query: str = Field(description="The optimal search query to use")
        rationale: str = Field(description="Explanation of why this query is optimal for this submodule")
    
    from langchain.output_parsers import PydanticOutputParser
    single_query_parser = PydanticOutputParser(pydantic_object=SingleSearchQueryOutput)
    
    try:
        result = await run_chain(prompt, lambda: get_llm(key_provider=google_key_provider), single_query_parser, {
            "user_topic": user_topic,
            "module_title": module_title,
            "module_description": module_description,
            "submodule_title": submodule_title,
            "submodule_description": submodule_description,
            "module_order": module_id + 1,
            "module_count": module_count,
            "submodule_order": sub_id + 1,
            "submodule_count": submodule_count,
            "module_context": module_context,
            "learning_path_context": learning_path_context,
            "output_language": output_language,
            "search_language": search_language,
            "format_instructions": single_query_parser.get_format_instructions()
        })
        
        # Create a single high-quality SearchQuery
        query = SearchQuery(
            keywords=result.query,
            rationale=result.rationale
        )
        
        logging.info(f"Generated optimal search query for submodule {sub_id+1}: {query.keywords}")
        return [query]  # Return as list for compatibility with existing code
    except Exception as e:
        logging.error(f"Error generating submodule search query: {str(e)}")
        # Create a fallback query
        fallback_query = SearchQuery(
            keywords=f"{module_title} {submodule_title} tutorial comprehensive guide",
            rationale="Fallback query due to error in query generation"
        )
        return [fallback_query]

async def execute_single_search_for_submodule(query: SearchQuery, key_provider=None) -> Dict[str, Any]:
    """
    Executes a single web search for a submodule using the Perplexity LLM.
    
    Args:
        query: A SearchQuery instance with keywords and rationale.
        key_provider: Optional key provider for Perplexity search.
        
    Returns:
        A dictionary with the query, rationale, and search results.
    """
    try:
        # Properly await the search_model coroutine
        search_model = await get_search_tool(key_provider=key_provider)
        logging.info(f"Searching for: {query.keywords}")
        
        # Create a prompt that asks for web search results
        search_prompt = f"{query.keywords}"
        
        # Invoke the Perplexity model with the search prompt as a string
        # Use ainvoke instead of invoke to properly leverage async execution
        result = await search_model.ainvoke(search_prompt)
        
        # Process the response into the expected format
        formatted_result = [
            {
                "source": f"Perplexity Search Result for '{query.keywords}'",
                "content": result.content
            }
        ]
        
        return {
            "query": query.keywords,
            "rationale": query.rationale,
            "results": formatted_result
        }
    except Exception as e:
        logging.error(f"Error searching for '{query.keywords}': {str(e)}")
        return {
            "query": query.keywords,
            "rationale": query.rationale,
            "results": [{"source": "Error", "content": f"Error performing search: {str(e)}"}]
        }

async def execute_submodule_specific_searches(
    state: LearningPathState, 
    module_id: int, 
    sub_id: int, 
    module: EnhancedModule, 
    submodule: Submodule,
    sub_queries: List[SearchQuery]
) -> List[Dict[str, Any]]:
    """
    Execute a single web search for a submodule.
    
    Args:
        state: The current LearningPathState.
        module_id: Index of the parent module.
        sub_id: Index of the submodule.
        module: The EnhancedModule instance.
        submodule: The Submodule instance.
        sub_queries: List containing the single search query for the submodule.
        
    Returns:
        A list containing a single search result dictionary.
    """
    logging.info(f"Executing search for submodule {sub_id+1} of module {module_id+1}: {submodule.title}")
    if not sub_queries or len(sub_queries) == 0:
        logging.warning("No search query available for submodule")
        return []
    
    # Take only the first query even if multiple are provided (for robustness)
    query = sub_queries[0]
    
    try:
        # Execute a single search
        result = await execute_single_search_for_submodule(query, key_provider=state.get("pplx_key_provider"))
        logging.info(f"Completed search for submodule {sub_id+1} with query: {query.keywords}")
        return [result]
    except Exception as e:
        logging.error(f"Error executing submodule search: {str(e)}")
        return []

async def develop_submodule_specific_content(
    state: LearningPathState, 
    module_id: int, 
    sub_id: int, 
    module: EnhancedModule, 
    submodule: Submodule,
    sub_queries: List[SearchQuery],
    sub_search_results: List[Dict[str, Any]]
) -> str:
    """
    Develops comprehensive content for a submodule using a single search result.
    
    Args:
        state: The current LearningPathState.
        module_id: Index of the parent module.
        sub_id: Index of the submodule.
        module: The EnhancedModule instance.
        submodule: The Submodule instance.
        sub_queries: List containing the single search query used.
        sub_search_results: List containing the single search result obtained.
        
    Returns:
        A string containing the developed submodule content.
    """
    logger = logging.getLogger("learning_path.content_developer")
    logger.info(f"Developing content for submodule {sub_id+1} of module {module_id+1}: {submodule.title}")
    if not sub_search_results:
        logger.warning("No search results available for content development")
        return "No content generated due to missing search results."
    
    # Get language information from state
    output_language = state.get('language', 'en')
    
    # Import escape function from helpers
    from core.graph_nodes.helpers import escape_curly_braces
    
    # Process the single search result
    formatted_results = ""
    result = sub_search_results[0]  # We expect only one result
    
    # Escapar las llaves en query y rationale
    query = escape_curly_braces(result.get('query', ''))
    rationale = escape_curly_braces(result.get('rationale', ''))
    
    formatted_results += f"Query: {query}\n"
    formatted_results += f"Rationale: {rationale}\nResults:\n"
    
    results = result.get("results", "")
    if isinstance(results, str):
        # Escapar las llaves en el contenido del resultado
        escaped_content = escape_curly_braces(results)
        formatted_results += f"  {escaped_content}\n\n"
    else:
        for item in results:
            # Escapar las llaves en cada campo del resultado
            title = escape_curly_braces(item.get("title", "No title"))
            content = escape_curly_braces(item.get("content", "No content"))
            url = escape_curly_braces(item.get("url", "No URL"))
            
            formatted_results += f"  - {title}: {content}\n    URL: {url}\n"
        formatted_results += "\n"
    
    # Continue with context building and content generation as before
    learning_path_context = ""
    for i, mod in enumerate(state.get("enhanced_modules") or []):
        indicator = " (CURRENT)" if i == module_id else ""
        # Escapar las llaves en la descripción
        mod_title = escape_curly_braces(mod.title)
        mod_desc = escape_curly_braces(mod.description)
        learning_path_context += f"Module {i+1}: {mod_title}{indicator}\n{mod_desc}\n"
    
    # Escapar las llaves en la información del módulo
    module_title = escape_curly_braces(module.title)
    module_desc = escape_curly_braces(module.description)
    module_context = f"Current Module: {module_title}\nDescription: {module_desc}\nAll Submodules:\n"
    
    for i, s in enumerate(module.submodules):
        indicator = " (CURRENT)" if i == sub_id else ""
        # Escapar las llaves en la información del submódulo
        sub_title = escape_curly_braces(s.title)
        sub_desc = escape_curly_braces(s.description)
        module_context += f"  {i+1}. {sub_title}{indicator}\n  {sub_desc}\n"
    
    adjacent_context = "Adjacent Submodules:\n"
    if sub_id > 0:
        prev = module.submodules[sub_id-1]
        # Escapar las llaves en la información del submódulo previo
        prev_title = escape_curly_braces(prev.title)
        prev_desc = escape_curly_braces(prev.description)
        adjacent_context += f"Previous: {prev_title}\n{prev_desc}\n"
    else:
        adjacent_context += "No previous submodule.\n"
    
    if sub_id < len(module.submodules) - 1:
        nxt = module.submodules[sub_id+1]
        # Escapar las llaves en la información del submódulo siguiente
        nxt_title = escape_curly_braces(nxt.title)
        nxt_desc = escape_curly_braces(nxt.description)
        adjacent_context += f"Next: {nxt_title}\n{nxt_desc}\n"
    else:
        adjacent_context += "No next submodule.\n"
    
    # Modificar el prompt para enfatizar trabajar con un solo resultado de búsqueda
    single_result_prompt = """
# EXPERT EDUCATIONAL CONTENT DEVELOPER INSTRUCTIONS

You are tasked with developing comprehensive educational content for a specific submodule based on a focused search result.

## SUBMODULE CONTEXT
- Learning Topic: {user_topic}
- Module: {module_title} (Module {module_order} of {module_count})
- Submodule: {submodule_title} (Submodule {submodule_order} of {submodule_count})
- Description: {submodule_description}

## MODULE CONTEXT
{module_context}

## ADJACENT CONTEXT
{adjacent_context}

## LEARNING PATH CONTEXT
{learning_path_context}

## LANGUAGE INSTRUCTIONS
Generate all content in {language}. This is the language selected by the user for learning the material.

## RESEARCH MATERIAL
The following is your research material from a single focused search:

{search_results}

## YOUR TASK

Based on this research material, develop comprehensive educational content for this submodule that:

1. Thoroughly covers the specific topic of the submodule
2. Aligns with the overall learning objectives of the module
3. Builds logically on previous submodules and prepares for subsequent ones
4. Includes relevant examples, explanations, and educational activities
5. Provides practical applications and theoretical foundations
6. Organizes information in a clear, structured learning progression

Your content should be:
- Comprehensive (1500-2000 words)
- Well-organized with clear headings and subheadings
- Rich in examples and explanations
- Written in an educational and engaging style
- Suitable for the specified depth level of the submodule

Format your response using Markdown, with clear section headings, code examples if relevant, and proper formatting for educational content.
"""
    
    prompt = ChatPromptTemplate.from_template(single_result_prompt)
    try:
        # Properly await the LLM coroutine
        llm = await get_llm(key_provider=state.get("google_key_provider"))
        output_parser = StrOutputParser()
        chain = prompt | llm | output_parser
        
        # Escapar las llaves en los campos de texto
        user_topic = escape_curly_braces(state["user_topic"])
        submodule_title = escape_curly_braces(submodule.title)
        submodule_description = escape_curly_braces(submodule.description)
        
        sub_content = await chain.ainvoke({
            "user_topic": user_topic,
            "module_title": module_title,
            "module_description": module_desc,
            "submodule_title": submodule_title,
            "submodule_description": submodule_description,
            "module_order": module_id + 1,
            "module_count": len(state.get("enhanced_modules") or []),
            "submodule_order": sub_id + 1,
            "submodule_count": len(module.submodules),
            "module_context": module_context,
            "adjacent_context": adjacent_context,
            "learning_path_context": learning_path_context,
            "search_results": formatted_results,
            "format_instructions": "",
            "language": output_language
        })
        
        if not sub_content:
            logger.error("LLM returned empty content")
            sub_content = f"Error: No content generated for {submodule.title}"
        elif not isinstance(sub_content, str):
            logger.warning("Content not a string; converting")
            sub_content = str(sub_content)
        return sub_content
    except Exception as e:
        logger.exception(f"Error developing submodule content: {str(e)}")
        return f"Error developing content: {str(e)}"

async def finalize_enhanced_learning_path(state: LearningPathState) -> Dict[str, Any]:
    """
    Finalizes the enhanced learning path with all processed submodules.
    
    Args:
        state: The current state with all processed submodules.
        
    Returns:
        Final state with the complete enhanced learning path.
    """
    logging.info("Finalizing enhanced learning path")
    
    progress_callback = state.get("progress_callback")
    if progress_callback:
        await progress_callback("Finalizing your learning path with all enhanced content...")
    
    logger = logging.getLogger("learning_path.finalizer")
    logger.info("Finalizing enhanced learning path with submodules")
    try:
        if not state.get("developed_submodules"):
            logger.warning("No developed submodules available")
            return {"final_learning_path": {"topic": state["user_topic"], "modules": []}, "steps": ["No submodules developed"]}
        module_to_subs = {}
        for sub in state["developed_submodules"]:
            module_to_subs.setdefault(sub.module_id, []).append(sub)
        for module_id in module_to_subs:
            module_to_subs[module_id].sort(key=lambda s: s.submodule_id)
        final_modules = []
        for module_id, module in enumerate(state.get("enhanced_modules") or []):
            subs = module_to_subs.get(module_id, [])
            submodule_data = []
            for sub in subs:
                summary = sub.summary if hasattr(sub, 'summary') else (sub.content[:200].strip() + "..." if sub.content else "")
                submodule_data.append({
                    "id": sub.submodule_id,
                    "title": sub.title,
                    "description": sub.description,
                    "content": sub.content,
                    "order": sub.submodule_id + 1,
                    "summary": summary,
                    "connections": getattr(sub, 'connections', {})
                })
            module_data = {
                "id": module_id,
                "title": module.title,
                "description": module.description,
                "core_concept": getattr(module, 'core_concept', ""),
                "learning_objective": getattr(module, 'learning_objective', ""),
                "prerequisites": getattr(module, 'prerequisites', []),
                "key_components": getattr(module, 'key_components', []),
                "expected_outcomes": getattr(module, 'expected_outcomes', []),
                "submodules": submodule_data
            }
            final_modules.append(module_data)
        final_learning_path = {"topic": state["user_topic"], "modules": final_modules, "execution_steps": state["steps"]}
        logger.info(f"Finalized learning path with {len(final_modules)} modules")
        if progress_callback:
            total_submodules = sum(len(m.submodules) for m in state.get("enhanced_modules", []))
            await progress_callback(f"Learning path complete with {len(final_modules)} modules and {total_submodules} detailed submodules")
        return {"final_learning_path": final_learning_path, "steps": ["Finalized enhanced learning path"]}
    except Exception as e:
        logger.exception(f"Error finalizing learning path: {str(e)}")
        return {"final_learning_path": {"topic": state["user_topic"], "modules": [], "error": str(e)}, "steps": [f"Error: {str(e)}"]}

def check_submodule_batch_processing(state: LearningPathState) -> str:
    """
    Checks if all submodule batches have been processed and provides detailed progress.
    
    Args:
        state: The current LearningPathState.
        
    Returns:
        "all_batches_processed" if all batches are done, otherwise "continue_processing".
    """
    current_index = state.get("current_submodule_batch_index")
    batches = state.get("submodule_batches")
    
    if current_index is None or batches is None:
        logging.warning("Submodule batch processing state is not properly initialized")
        return "all_batches_processed"
    
    if current_index >= len(batches):
        # All batches processed, provide summary statistics
        total_processed = len(state.get("developed_submodules", []))
        total_batches = len(batches)
        
        # Get completed submodules with timing information
        submodules_in_process = state.get("submodules_in_process", {})
        successful_submodules = [v for k, v in submodules_in_process.items() 
                                if v.get("status") == "completed"]
        error_submodules = [v for k, v in submodules_in_process.items() 
                           if v.get("status") == "error"]
        
        # Calculate timing statistics if available
        timing_stats = "No timing data available"
        if successful_submodules and "processing_time" in successful_submodules[0]:
            total_times = [s["processing_time"]["total"] for s in successful_submodules 
                          if "processing_time" in s]
            if total_times:
                avg_time = sum(total_times) / len(total_times)
                min_time = min(total_times)
                max_time = max(total_times)
                timing_stats = f"Average: {avg_time:.2f}s, Min: {min_time:.2f}s, Max: {max_time:.2f}s"
        
        # Log detailed completion information
        logger = logging.getLogger("learning_path.batch_processor")
        logger.info(f"All {total_batches} submodule batches processed successfully")
        logger.info(f"Completed {total_processed} submodules with {len(error_submodules)} errors")
        logger.info(f"Processing time statistics: {timing_stats}")
        
        # DO NOT create an asyncio task directly, as this might be called in a sync context
        # Just log the completion message
        logger.info(f"All {total_processed} submodules across {total_batches} batches completed")
        
        return "all_batches_processed"
    else:
        # Calculate progress percentage
        progress_pct = int((current_index / len(batches)) * 100)
        
        # Calculate submodules processed so far
        processed_count = 0
        total_count = 0
        submodules_in_process = state.get("submodules_in_process", {})
        for batch in batches[:current_index]:
            for module_id, sub_id in batch:
                total_count += 1
                key = (module_id, sub_id)
                if key in submodules_in_process:
                    if submodules_in_process[key].get("status") in ["completed", "error"]:
                        processed_count += 1
        
        # Log progress information
        remaining = len(batches) - current_index
        logger = logging.getLogger("learning_path.batch_processor")
        logger.info(f"Continue processing: batch {current_index+1} of {len(batches)} ({progress_pct}% complete)")
        logger.info(f"Processed {processed_count}/{total_count} submodules so far, {remaining} batches remaining")
        
        # DO NOT create an asyncio task directly
        # Just log the progress without using a callback
        logger.info(f"Progress: {progress_pct}% - Processing batch {current_index+1} of {len(batches)}")
        
        return "continue_processing"
