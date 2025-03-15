import logging
from langgraph.graph import StateGraph, START, END
from models import LearningPathState
from graph_nodes import (
    generate_search_queries,
    execute_web_searches,
    create_learning_path,
    plan_submodules,
    initialize_submodule_processing,
    process_submodule_batch,
    finalize_enhanced_learning_path,
    check_submodule_batch_processing
)

def build_graph():
    """
    Construct and return the enhanced LangGraph with hierarchical submodule processing.
    """
    logging.info("Building graph with hierarchical submodule processing")
    graph = StateGraph(LearningPathState)
    
    # Add nodes for initial learning path generation
    graph.add_node("generate_search_queries", generate_search_queries)
    graph.add_node("execute_web_searches", execute_web_searches)
    graph.add_node("create_learning_path", create_learning_path)
    
    # Add nodes for submodule planning and development
    graph.add_node("plan_submodules", plan_submodules)
    graph.add_node("initialize_submodule_processing", initialize_submodule_processing)
    graph.add_node("process_submodule_batch", process_submodule_batch)
    graph.add_node("finalize_enhanced_learning_path", finalize_enhanced_learning_path)
    
    # Connect initial learning path flow
    graph.add_edge(START, "generate_search_queries")
    graph.add_edge("generate_search_queries", "execute_web_searches")
    graph.add_edge("execute_web_searches", "create_learning_path")
    
    # Connect submodule planning and development flow
    graph.add_edge("create_learning_path", "plan_submodules")
    graph.add_edge("plan_submodules", "initialize_submodule_processing")
    graph.add_edge("initialize_submodule_processing", "process_submodule_batch")
    graph.add_conditional_edges(
        "process_submodule_batch",
        check_submodule_batch_processing,
        {
            "all_batches_processed": "finalize_enhanced_learning_path",
            "continue_processing": "process_submodule_batch"
        }
    )
    graph.add_edge("finalize_enhanced_learning_path", END)
    
    return graph.compile()
