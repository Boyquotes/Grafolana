import networkx as nx
from typing import Dict, List, Optional, Set, Tuple, Any

from GrafolanaBack.domain.transaction.models.account import AccountVertex
from GrafolanaBack.domain.transaction.models.graph import TransactionGraph, TransferType, TransferProperties
from GrafolanaBack.domain.transaction.models.swap import Swap, TransferAccountAddresses
from GrafolanaBack.domain.transaction.models.transaction_context import TransactionContext
from GrafolanaBack.domain.transaction.repositories.account_repository import AccountRepository
from GrafolanaBack.domain.logging.logging import logger
from GrafolanaBack.domain.transaction.services.graph_builder_service import GraphBuilderService

class SwapResolverService:
    """
    Service for resolving swap paths in transaction graphs.
    
    This service analyzes the graph to determine how tokens flow through
    a swap operation, including amount in, amount out, and fees.
    """

    def __init__(self, accountRepository: AccountRepository):
        self.accountRepository = accountRepository
    
    def resolve_swap_paths(self, transaction_context: TransactionContext) -> None:
        """
        Resolve paths for a swap operation in the graph.
        
        This adds a direct edge between pool accounts to represent the swap.
        
        Args:
            graph: The transaction graph
            swap: The swap operation to resolve
        """
        """Resolve swap paths in the transaction graph."""

        failed_swaps: List[int] = []

        # For each swap, find paths between accounts
        swap: Swap
        # First resolve all swaps that are not router swaps
        for swap in transaction_context.swaps:
            if not swap.router:
                if not self.resolve_swap(transaction_context, swap):
                    failed_swaps.append(swap.id)
        
        # Then resolve all router swaps using the path resolved from normal swaps
        for swap in transaction_context.swaps:
            if swap.router:
                if not self.resolve_router_swap_paths(transaction_context, swap):
                    failed_swaps.append(swap.id)
        
        # Remove failed swaps from the transaction context
        transaction_context.swaps = [swap for swap in transaction_context.swaps if swap.id not in failed_swaps] 

        

    def resolve_router_swap_paths(self, transaction_context: TransactionContext, router_swap: Swap) -> bool:
        """
        Resolve paths for a router swap operation in the graph.
        
        This adds a virtual program account and 2 edges:
        - from the user source account to the router program account
        - from the router program account to the user destination account
        
        Args:
            transaction_context: The transaction context containing the graph
            router_swap: The router_swap swap operation to resolve
        """
        subgraph = transaction_context.graph.create_subgraph_for_swap(router_swap)
        if subgraph is None:
            logger.error(f"No subgraph found for router swap {router_swap.id}, tx: {transaction_context.transaction_signature}")
            return False

        # Find first link of type SWAP_INCOMING by selecting min key
        # and then the first link of type SWAP_OUTGOING by selecting max key
        # This is done to find the first and last transfer in the swap
        # and to calculate the amount_in and amount_out
        # So we can add the virtual program account and the two edges
        
        # Get all edges with data and filter by transfer_type
        all_edges: List[Tuple[AccountVertex, AccountVertex, int, Dict[str, Any]]] = list(subgraph.edges(data=True, keys=True))
        
        if not all_edges:
            logger.error(f"No edges found in subgraph for router swap {router_swap.id}, tx: {transaction_context.transaction_signature}")
            return False
        
        # Filter for SWAP_INCOMING edges and find the one with minimum key
        incoming_edges = [(u, v, k, data) for u, v, k, data in all_edges 
                         if data.get("transfer_type") == TransferType.SWAP_INCOMING]
        if not incoming_edges:
            logger.warning(f"No SWAP_INCOMING edges found for router swap {router_swap.id} transaction {transaction_context.transaction_signature}")
            return False
        
        swap_incoming_edge = min(incoming_edges, key=lambda edge: int(edge[2]))
        incoming_source, incoming_target, incoming_key, incoming_data = swap_incoming_edge
        amount_in = incoming_data["amount_source"]  # Use the proper field from the edge data
        
        # Filter for SWAP_OUTGOING edges and find the one with maximum key
        outgoing_edges = [(u, v, k, data) for u, v, k, data in all_edges 
                         if data.get("transfer_type") == TransferType.SWAP_OUTGOING]
        if not outgoing_edges:
            logger.warning(f"No SWAP_OUTGOING edges found for router swap {router_swap.id}")
            return False
        
        swap_outgoing_edge = max(outgoing_edges, key=lambda edge: int(edge[2]))
        outgoing_source, outgoing_target, outgoing_key, outgoing_data = swap_outgoing_edge
        amount_out = outgoing_data["amount_destination"]  # Use the proper field from the edge data

        swap_program_account =  GraphBuilderService.prepare_swap_program_account(
            transaction_context = transaction_context,
            program_address = router_swap.program_address,
        )

        router_swap.program_account_vertex = swap_program_account.get_vertex()

        # # Get all vertices with the relevant addresses
        # user_source_vertices = [v for v in subgraph.nodes() if v.address == router_swap.get_user_source()]
        # user_dest_vertices = [v for v in subgraph.nodes() if v.address == router_swap.get_user_destination()]
        
        # # Find the best source and destination vertices
        # # Usually the earliest version for source (before swap happens) and 
        # # latest version for destination (after swap completes)
        # user_source_vertex = min(user_source_vertices, key=lambda v: v.version) if user_source_vertices else None
        # user_dest_vertex = max(user_dest_vertices, key=lambda v: v.version) if user_dest_vertices else None
        # if (user_source_vertex is None) or (user_dest_vertex is None):
        #     logger.error(f"user vertices not found for swap {router_swap}, source: {user_source_vertex}, destination: {user_dest_vertex}, tx: {transaction_context.transaction_signature}")
        #     return
        
        # Add virtual transfer from user source to swap_program_account 
        transaction_context.graph.add_edge(
                    source = incoming_source, 
                    target = swap_program_account.get_vertex(),
                    transfer_properties = TransferProperties(
                        transfer_type = TransferType.SWAP_ROUTER_INCOMING,
                        program_address = router_swap.program_address,
                        amount_source = amount_in,
                        amount_destination = amount_in,
                        swap_id = router_swap.id,
                        swap_parent_id= router_swap.id,
                        parent_router_swap_id = router_swap.id,
                    ),
                    key = incoming_key + 1)
        
        # Add virtual transfer from swap_program_account to user destination
        transaction_context.graph.add_edge(
                    source = swap_program_account.get_vertex(), 
                    target = outgoing_target,
                    transfer_properties = TransferProperties(
                        transfer_type = TransferType.SWAP_ROUTER_OUTGOING,
                        program_address = router_swap.program_address,
                        amount_source = amount_out,
                        amount_destination = amount_out,
                        swap_id = router_swap.id,
                        swap_parent_id= router_swap.id,
                        parent_router_swap_id = router_swap.id,
                    ),
                    key = outgoing_key - 1)
        
        logger.debug(f"Resolved swap {router_swap.id} with amount_in={amount_in}, amount_out={amount_out}, fee={router_swap.fee}, tx: {transaction_context.transaction_signature}")
    
        return True
        

    def resolve_swap(self, transaction_context: TransactionContext, swap: Swap) -> bool:
        """
        Resolve a swap operation in the transaction graph.
        """

        subgraph = transaction_context.graph.create_subgraph_for_swap(swap)
        
        # Get all vertices with the relevant addresses
        user_source_vertices = [v for v in subgraph.nodes() if v.address == swap.get_user_source()]
        user_dest_vertices = [v for v in subgraph.nodes() if v.address == swap.get_user_destination()]
        
        # Find the best source and destination vertices
        # Usually the earliest version for source (before swap happens) and 
        # latest version for destination (after swap completes)
        user_source_vertex: AccountVertex = min(user_source_vertices, key=lambda v: v.version) if user_source_vertices else None
        user_dest_vertex: AccountVertex = max(user_dest_vertices, key=lambda v: v.version) if user_dest_vertices else None
        if (user_source_vertex is None) or (user_dest_vertex is None):
            logger.error(f"user vertices not found for swap {swap.id}, source: {user_source_vertex.address}, destination: {user_dest_vertex.address}, tx: {transaction_context.transaction_signature}")
            return False

        swap_pools : List[AccountVertex]= []
        # If pools are stored as source/destination
        if isinstance(swap.pool_addresses, TransferAccountAddresses):
            swap_pools.extend([v for v in subgraph.nodes() if v.address == swap.pool_addresses.destination])
            swap_pools.extend([v for v in subgraph.nodes() if v.address == swap.pool_addresses.source])
        # If pools are stored as a list of pools
        else:
            swap_pools = [v for v in subgraph.nodes() if v.address in swap.pool_addresses]
        # Search through list of pool's addresses for paths:
        #  - from user_source 
        #  - to user_destination
        pool_dest_vertices = []
        pool_source_vertices = []
        
        # Might not be perfect..
        for pool in swap_pools:
            # Set is_pool to True for all pools in the mapping
            self.accountRepository.accounts.get(pool.address).is_pool = True
            if nx.has_path(subgraph,user_source_vertex, pool):
                pool_dest_vertices.append(pool)
            if nx.has_path(subgraph,pool, user_dest_vertex):
                pool_source_vertices.append(pool)

        pool_dest_vertex: AccountVertex = max(pool_dest_vertices, key=lambda v: v.version) if pool_dest_vertices else None
        pool_source_vertex: AccountVertex = min(pool_source_vertices, key=lambda v: v.version) if pool_source_vertices else None
        if (pool_dest_vertex is None) or (pool_source_vertex is None):
            logger.error(f"pool vertices not found for swap {swap.id}, source: {user_source_vertex.address}, destination: {user_dest_vertex.address}, tx: {transaction_context.transaction_signature}")
            return False

        logger.debug(f"finding paths for: user_source_vertex: {user_source_vertex.address}, pool_dest_vertex: {pool_dest_vertex.address}")
        # Find path from user_source to pool_destination
        try:
            path_a = nx.shortest_path(subgraph, user_source_vertex, pool_dest_vertex)
            if len(path_a) < 2:
                logger.error(f"path user -> pool too short for swap {swap.id}, source: {user_source_vertex.address}, destination: {pool_dest_vertex.address}, tx: {transaction_context.transaction_signature}")
                return False
            _ , _ , data = transaction_context.graph.get_last_transfer(path_a, subgraph)
            amount_in = sum(edge_data["amount_destination"] for edge_data in data.values())
            
            # Create a new transfer key for the swap
            # We take the key of the transfer before the swap, and add 1 to it
            swap_transfer_key = int(list(data.keys())[0]) + 5

        except nx.NetworkXNoPath:
            # Handle case where path doesn't exist
            logger.error(f"path doesn't exist for swap {swap.id}, source: {user_source_vertex.address}, destination: {pool_dest_vertex.address}, tx: {transaction_context.transaction_signature}")
            return False

        logger.debug(f"finding paths for: pool_source_vertex: {pool_source_vertex.address}, user_dest_vertex: {user_dest_vertex.address}")
        # Find path from pool_source to user_destination
        try:
            path_b = nx.shortest_path(subgraph, pool_source_vertex, user_dest_vertex)
            if len(path_b) < 2:
                logger.error(f"path pool -> user too short for swap {swap.id}, source: {pool_source_vertex.address}, destination: {user_dest_vertex.address}, tx: {transaction_context.transaction_signature}")
                return False
            _ , _ , data = transaction_context.graph.get_first_transfer(path_b, subgraph)
            real_swap_amount_out = sum(edge_data["amount_source"] for edge_data in data.values())

            # Calculate amount_out by summing the amount_source of all edges with swap.user_addresses.destination as destination 
            # minus the sum of all edges with swap.user_addresses.destination as source
            # don't count edges where swap.user_addresses.destination is both source and destination
            amount_out = 0
            source: AccountVertex
            destination: AccountVertex
            for source, destination, data in subgraph.edges(data=True):
                if source.address==swap.user_addresses.destination and destination.address != swap.user_addresses.destination:
                    amount_out -= data["amount_source"]
                if destination.address==swap.user_addresses.destination and source.address != swap.user_addresses.destination:
                    amount_out += data["amount_source"]
                

        except nx.NetworkXNoPath:
            # Handle case where path doesn't exist
            logger.error(f"path doesn't exist for swap {swap.id}, source: {pool_source_vertex.address}, destination: {user_dest_vertex.address}, tx: {transaction_context.transaction_signature}")
            return False
        
        swap.fee = real_swap_amount_out - amount_out

        # Add virtual transfer from pool to pool to represent the swap
        transaction_context.graph.add_edge(
                    source = pool_dest_vertex, 
                    target = pool_source_vertex,
                    transfer_properties = TransferProperties(
                        transfer_type = TransferType.SWAP,
                        program_address = swap.program_address,
                        amount_source = amount_in,
                        amount_destination = amount_out,
                        swap_id = swap.id,
                        swap_parent_id= swap.id,
                        parent_router_swap_id = swap.parent_router_swap_id,
                    ),
                    key = swap_transfer_key)
        
        swap_program_account =  GraphBuilderService.prepare_swap_program_account(
            transaction_context = transaction_context,
            program_address = swap.program_address,
        )

        swap.program_account_vertex = swap_program_account.get_vertex()

        # get lower key of all the swap's edges
        swap_incoming_transfer_key = min([int(key) for _,_,key in subgraph.edges(keys=True)]) if len(subgraph.edges(keys=True)) > 0 else 0

        # get maximum key of all the swap's edges
        swap_outgoing_transfer_key = max([int(key) for _,_,key in subgraph.edges(keys=True)]) if len(subgraph.edges(keys=True)) > 0 else 0

        # Add virtual transfer from user source to swap_program_account 
        transaction_context.graph.add_edge(
                    source = user_source_vertex, 
                    target = swap_program_account.get_vertex(),
                    transfer_properties = TransferProperties(
                        transfer_type = TransferType.SWAP_INCOMING,
                        program_address = swap.program_address,
                        amount_source = amount_in,
                        amount_destination = amount_in,
                        swap_id = swap.id,
                        swap_parent_id= swap.id,
                        parent_router_swap_id = swap.parent_router_swap_id,
                    ),
                    key = swap_incoming_transfer_key + 1 )
        
        # Add virtual transfer from swap_program_account to user destination
        transaction_context.graph.add_edge(
                    source = swap_program_account.get_vertex(), 
                    target = user_dest_vertex,
                    transfer_properties = TransferProperties(
                        transfer_type = TransferType.SWAP_OUTGOING,
                        program_address = swap.program_address,
                        amount_source = amount_out,
                        amount_destination = amount_out,
                        swap_id = swap.id,
                        swap_parent_id= swap.id,
                        parent_router_swap_id = swap.parent_router_swap_id,
                    ),
                    key = swap_outgoing_transfer_key - 1)
        
        logger.debug(f"Resolved swap {swap.id} with amount_in={amount_in}, amount_out={amount_out}, fee={swap.fee}, tx: {transaction_context.transaction_signature}")

        return True

    def _calculate_amount_in_from_balance_changes(self, graph: TransactionGraph, swap: Swap) -> int:
        """
        Calculate amount sent to a swap by analyzing balance changes in accounts.
        
        This is used as a fallback when path finding fails.
        
        Args:
            graph: The transaction graph
            swap: The swap to analyze
            
        Returns:
            The amount sent to the swap (amount_in)
        """
        # Find all vertices with the user's source address
        user_source_vertices = graph.get_nodes_by_address(swap.get_user_source())
        if len(user_source_vertices) < 2:
            return 0
            
        # Sort by version and take first and last
        user_source_vertices.sort(key=lambda v: v.version)
        first_vertex = user_source_vertices[0]
        last_vertex = user_source_vertices[-1]
        
        # Calculate balance difference
        amount_in = 0
        
        # Find all outgoing transfer edges from source account to non-source accounts
        for u, v, k, data in graph.graph.edges(data=True, keys=True):
            if u.address == swap.get_user_source() and v.address != swap.get_user_source():
                if data.get("swap_parent_id") == swap.id:
                    amount_in += data["amount_source"]
        
        return amount_in
    
    def _calculate_amount_out_from_balance_changes(self, graph: TransactionGraph, swap: Swap) -> int:
        """
        Calculate amount received from a swap by analyzing balance changes in accounts.
        
        This is used as a fallback when path finding fails.
        
        Args:
            graph: The transaction graph
            swap: The swap to analyze
            
        Returns:
            The amount received from the swap (amount_out)
        """
        # Find all vertices with the user's destination address
        user_dest_vertices = graph.get_nodes_by_address(swap.get_user_destination())
        if len(user_dest_vertices) < 2:
            return 0
            
        # Sort by version and take first and last
        user_dest_vertices.sort(key=lambda v: v.version)
        first_vertex = user_dest_vertices[0]
        last_vertex = user_dest_vertices[-1]
        
        # Calculate amount out by summing incoming transfers to destination account
        amount_out = 0
        
        # Find all incoming transfer edges to destination account from non-destination accounts
        for u, v, k, data in graph.graph.edges(data=True, keys=True):
            if v.address == swap.get_user_destination() and u.address != swap.get_user_destination():
                if data.get("swap_parent_id") == swap.id:
                    amount_out += data["amount_destination"]
        
        return amount_out