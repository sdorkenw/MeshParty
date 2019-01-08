from scipy import sparse
import numpy as np
import time
from meshparty import trimesh_vtk
from meshparty import trimesh_io
import pandas as pd
from scipy.spatial import cKDTree as KDTree
import pcst_fast  

def get_path(root, target, pred):
    path = [target]
    p = target
    while p != root:
        p = pred[p]
        path.append(p)
    path.reverse()
    return path


def find_far_points(trimesh):
    d = 0
    dn = 1
    a = 0
    b = 1
    k = 0
    pred = None
    ds = None
    while 1:
        k += 1
        dsn, predn = sparse.csgraph.dijkstra(
            trimesh.csgraph, False, a, return_predecessors=True)
        bn = np.argmax(dsn)
        dn = dsn[bn]
        if dn > d:
            b = a
            a = bn
            d = dn
            pred = predn
            ds = dsn
        else:
            break

    return b, a, pred, d, ds


def setup_root(mesh, soma_pos=None, soma_thresh=7500):
    valid = np.ones(len(mesh.vertices), np.bool)

    # soma mode
    if soma_pos is not None:
        # pick the first soma as root
        soma_d, soma_i = mesh.kdtree.query(soma_pos,
                                           k=len(mesh.vertices),
                                           distance_upper_bound=soma_thresh)
        assert(np.min(soma_d)<soma_thresh)
        root = soma_i[np.argmin(soma_d)]
        valid[soma_i[~np.isinf(soma_d)]] = False
        root_ds, pred = sparse.csgraph.dijkstra(mesh.csgraph,
                                                False,
                                                root,
                                                return_predecessors=True)
    else:
        # there is no soma so use far point heuristic
        root, target, pred, dm, root_ds = find_far_points(mesh)
        valid[root] = 0

    return root, root_ds, pred, valid


def recenter_verts(verts, edges, centers):
    edge_df = pd.DataFrame()
    edge_df['start_edge']=np.array(edges[:,0], np.int64)
    edge_df['end_edge']=np.array(edges[:,1], np.int64)
    edge_df['center_x']=np.array(centers)[:,0]
    edge_df['center_y']=np.array(centers)[:,1]
    edge_df['center_z']=np.array(centers)[:,2]
    start_mean=edge_df.groupby('start_edge').mean()[['center_x','center_y','center_z']]
    new_verts = np.copy(verts)
    new_verts[start_mean.index.values,:]=start_mean.values
    return new_verts


def skeletonize_axon(mesh_meta, axon_id, invalidation_d=5000, smooth_neighborhood=5,
                     max_tip_d=2000, large_skel_path_threshold=5000):
    axon_trimesh = mesh_meta.mesh(seg_id=axon_id, merge_large_components=False)
    axon_trimesh.stitch_overlapped_components()

    all_paths, roots, tot_path_lengths = skeletonize_components(axon_trimesh, invalidation_d=invalidation_d)
    tot_edges = merge_tips(axon_trimesh, all_paths, roots, tot_path_lengths,
                           large_skel_path_threshold=large_skel_path_threshold, max_tip_d=max_tip_d)
   
    skel_verts, skel_edges = trimesh_vtk.remove_unused_verts(axon_trimesh.vertices, tot_edges)
    smooth_verts = smooth_graph(skel_verts, skel_edges, neighborhood=smooth_neighborhood)

    cross_sections, centers = trimesh_vtk.calculate_cross_sections(axon_trimesh, smooth_verts, skel_edges)
    center_verts = recenter_verts(smooth_verts, skel_edges, centers)
    smooth_center_verts = smooth_graph(center_verts, skel_edges, neighborhood=smooth_neighborhood)

    return skel_verts, skel_edges, cross_sections, smooth_verts, smooth_center_verts


def merge_tips(mesh, all_paths, roots, tot_path_lengths,
               large_skel_path_threshold=5000, max_tip_d=2000):
    
    # collect all the tips of the skeletons (including roots)
    skel_tips = []
    for paths, root in zip(all_paths, roots):
        tips = []
        for path in paths:
            tip = mesh.vertices[path[0], :]
            tips.append(tip)
        root_tip = mesh.vertices[root, :]
        tips.append(root_tip)
        skel_tips.append(np.vstack(tips))
    # this is our overall tip matrix merged together
    all_tips = np.vstack(skel_tips)
    # variable to keep track of what component each tip was from
    tip_component = np.zeros(all_tips.shape[0])
    # counter to keep track of an overall tip index as we go through 
    # the components with different numbers of tips
    ind_counter = 0

    # setup the prize collection steiner forest problem variables
    # prizes will be related to path length of the tip components
    tip_prizes = [] 
    # where to collect all the tip<>tip edges
    all_edges = []
    # where to collect all the tip<>tip edge weights
    all_edge_weights = []

    # loop over all the components and their tips
    for k, tips, path_lengths in zip(range(len(tot_path_lengths)), skel_tips, tot_path_lengths):
        # how many tips in this component
        ntips = tips.shape[0]
        # calculate the total path length in this component
        path_len = np.sum(np.array(path_lengths))
        # the prize is 0 if this is small, and the path length if big
        prize = path_len if path_len > large_skel_path_threshold else 0
        # the cost of traveling within a skeleton is 0 if big, and the path_len if small
        cost = path_len if path_len <= large_skel_path_threshold else 0
        # add a block of prizes to the tip prizes for this component
        tip_prizes.append(prize*np.ones(ntips))
        # make an array of overall tip index for this component
        comp_tips = np.arange(ind_counter, ind_counter+ntips, dtype=np.int64)
        # add edges between this components root and each of the tips
        root_tips = (ind_counter+ntips-1)*np.ones(ntips, dtype=np.int64)
        in_tip_edges = np.hstack([root_tips[:, np.newaxis],
                                  comp_tips[:, np.newaxis]])
        all_edges.append(in_tip_edges)

        # add a block for the cost of these edges
        all_edge_weights.append(cost*np.ones(ntips))
        # note what component each of these tips is from
        tip_component[comp_tips] = k
        # increment our overall index counter
        ind_counter += ntips
    # gather all the prizes into a single block
    tip_prizes = np.concatenate(tip_prizes)

    # make a kdtree with all the tips
    tip_tree = KDTree(all_tips)
    
    # find the tips near one another
    close_tips = tip_tree.query_pairs(max_tip_d, output_type='ndarray')
    # filter out close tips from the same component
    diff_comp = ~(tip_component[close_tips[:, 0]] == tip_component[close_tips[:, 1]])
    filt_close_tips = close_tips[diff_comp]

    # add these as edges
    all_edges.append(filt_close_tips)
    # with weights equal to their euclidean distance
    dv = np.linalg.norm(all_tips[filt_close_tips[:,0],:] - all_tips[filt_close_tips[:, 1]], axis=1)
    all_edge_weights.append(dv)

    # consolidate the edges and weights into a single array
    inter_tip_weights = np.concatenate(all_edge_weights)
    inter_tip_edges = np.concatenate(all_edges)

    # run the prize collecting steiner forest optimization
    mst_verts, mst_edges = pcst_fast.pcst_fast(
        inter_tip_edges, tip_prizes, inter_tip_weights, -1, 1, 'gw', 1)
    # find the set of mst edges that are between connected components
    new_mst_edges = mst_edges[tip_component[inter_tip_edges[mst_edges, 0]] != tip_component[inter_tip_edges[mst_edges ,1]]]
    good_inter_tip_edges = inter_tip_edges[new_mst_edges, :]
    # use a kdtree to get their original mesh indices
    zero_d, orig_all_tip_inds = mesh.kdtree.query(all_tips, k=1)
    new_edges_orig_ind = orig_all_tip_inds[good_inter_tip_edges]

    # collect all the edges for all the paths into a single list
    # with the original indices of the mesh
    orig_edges = []
    for paths, root in zip(all_paths, roots):
        edges = paths_to_edges(paths)
        orig_edges.append(edges)
    orig_edges = np.vstack(orig_edges)
    # and add our new mst edges
    tot_edges = np.vstack([orig_edges, new_edges_orig_ind])

    return tot_edges


# def fix_skeleton(verts, edges):
#     # fix the skeleton so that it    
#     # filter out the vertices to be just those included in this skeleton
#     skel_verts, skel_edges = trimesh_vtk.remove_unused_verts(axon_trimesh.vertices, tot_edges)
#     Nind = skel_verts.shape[0]
#     g=sparse.csc_matrix((np.ones(len(skel_edges)), (skel_edges[:,0], skel_edges[:,1])), shape =
#                         (Nind,Nind))

#     # figure out how many skeleton components we have now
#     n_skel_comp, skel_labels = sparse.csgraph.connected_components(g,directed=False, return_labels=True)
#     skel_comp_labels, skel_comp_counts = np.unique(skel_labels, return_counts = True)
#     larg_skel_cc_ind = np.where(skel_comp_counts>100)[0]
#     large_skel_components=len(larg_skel_cc_ind)

def skeletonize_components(mesh, soma_pos=None, soma_thresh=7500, invalidation_d=10000):
    # find all the connected components in the mesh
    n_components, labels = sparse.csgraph.connected_components(mesh.csgraph,
                                                               directed=False,
                                                               return_labels=True)
    # variables to collect the paths, roots and path lengths
    all_paths = []
    roots = []
    tot_path_lengths = []

    # loop over the components
    for k in range(n_components):

        # get the mesh vertices involved in this component
        vert_inds = np.where(labels == k)[0]
        # filter out the triangles and edges involved
        filt_tris = mesh._filter_faces(vert_inds)[0]
        if mesh.mesh_edges is not None:
            filt_edges = mesh._filter_mesh_edges(vert_inds)[0]
        else:
            filt_edges = None
        # get reindexed vertices
        verts = mesh.vertices[vert_inds, :]

        # initialize a trimesh object
        mesh_component = trimesh_io.Mesh(verts,
                                         filt_tris,
                                         mesh_edges=filt_edges,
                                         process=False)

        # find the root using a soma position if you have it
        # it will fall back to a heuristic if the soma
        # is too far away for this component
        root, root_ds, pred, valid = setup_root(mesh_component,
                                                soma_pos=soma_pos,
                                                soma_thresh=soma_thresh)
        
        # run teasar on this component
        paths, path_lengths = mesh_teasar(mesh_component,
                                          root=root,
                                          root_ds=root_ds,
                                          root_pred=pred,
                                          valid=valid,
                                          invalidation_d=invalidation_d)
        # convert the paths into numpy arrays in the original index space
        for i, path in enumerate(paths):
            path = np.array(path)
            orig_path = vert_inds[path]
            paths[i] = orig_path
        # also convert the root
        root = vert_inds[root]

        # collect the results in lists
        tot_path_lengths.append(path_lengths)
        all_paths.append(paths)
        roots.append(root)
 
    return all_paths, roots, tot_path_lengths


def mesh_teasar(mesh, root=None, valid=None, root_ds=None, root_pred=None, soma_pt=None,
                soma_thresh=7500, invalidation_d=10000, return_timing=False):
    # if no root passed, then calculation one
    if root is None:
        root, root_ds, root_pred, valid = setup_root(mesh,
                                                soma_pos=soma_pt,
                                                soma_thresh=soma_thresh)
    # if root_ds have not be precalculated do so
    if root_ds is None:
        root_ds, root_pred = sparse.csgraph.dijkstra(mesh.csgraph,
                                                False,
                                                root,
                                                return_predecessors=True)
    # if certain vertices haven't been pre-invalidated start with just
    # the root vertex invalidated
    if valid is None:
        valid = np.ones(len(mesh.vertices), np.bool)
        valid[root] = False
    else:
        if (len(valid) != len(mesh.vertices)):
            raise Exception("valid must be length of vertices")

    if not np.all(~np.isinf(root_ds)):
        print(np.where(np.isinf(root_ds)))
        raise Exception("all points should be reachable from root")

    # vector to store each branch result
    paths = []

    # vector to store each path's total length
    path_lengths = []

    # keep track of the nodes that have been visited
    visited_nodes = [root]

    # counter to track how many branches have been counted
    k = 0

    # arrays to track timing
    start = time.time()
    time_arrays = [[], [], [], [], []]

    # keep looping till all vertices have been invalidated
    while(np.sum(valid) > 0):
        k += 1

        t = time.time()
        # find the next target, farthest vertex from root
        # that has not been invalidated
        target = np.argmax(root_ds*valid)
        time_arrays[0].append(time.time()-t)

        t = time.time()
        # figure out the longest this branch could be
        # by following the route from target to the root
        # and finding the first already visited node (max_branch)
        # The dist(root->target) - dist(root->max_branch)
        # is the maximum distance the shortest route to a branch
        # point from the target could possibly be,
        # use this bound to reduce the djisktra search radius for this target
        max_branch = target
        while max_branch not in visited_nodes:
            max_branch = root_pred[max_branch]
        max_path_length = root_ds[target]-root_ds[max_branch]

        # calculate the shortest path to that vertex
        # from all other vertices
        # up till the distance to the root
        ds, pred_t = sparse.csgraph.dijkstra(
            mesh.csgraph,
            False,
            target,
            limit=max_path_length,
            return_predecessors=True)

        # pick out the vertex that has already been visited
        # which has the shortest path to target
        min_node = np.argmin(ds[visited_nodes])
        # reindex to get its absolute index
        branch = visited_nodes[min_node]
        # this is in the index of the point on the skeleton
        # we want this branch to connect to
        time_arrays[1].append(time.time()-t)

        t = time.time()
        # get the path from the target to branch point
        path = get_path(target, branch, pred_t)
        visited_nodes += path[0:-1]
        # record its length
        assert(~np.isinf(ds[branch]))
        path_lengths.append(ds[branch])
        # record the path
        paths.append(path)
        time_arrays[2].append(time.time()-t)

        t = time.time()
        # get the distance to all points along the new path
        # within the invalidation distance
        dm = sparse.csgraph.dijkstra(
            mesh.csgraph, False, path, limit=invalidation_d, multi_target=True)
        time_arrays[3].append(time.time()-t)

        t = time.time()
        # all such non infinite distances are within the invalidation
        # zone and should be marked invalid
        valid[~np.isinf(dm)] = False
        # print out how many vertices are still valid
        print(np.sum(valid))
        time_arrays[4].append(time.time()-t)
    # record the total time
    dt = time.time() - start

    if return_timing:
        return paths, path_lengths, time_arrays, dt
    else:
        return paths, path_lengths


def setup_root(mesh, soma_pos=None, soma_thresh=7500):
    valid = np.ones(len(mesh.vertices), np.bool)
    root = None
    # soma mode
    if soma_pos is not None:
        # pick the first soma as root
        soma_d, soma_i = mesh.kdtree.query(soma_pos,
                                           k=len(mesh.vertices),
                                           distance_upper_bound=soma_thresh)
        if (np.min(soma_d) < soma_thresh):
            root = soma_i[np.argmin(soma_d)]
            valid[soma_i[~np.isinf(soma_d)]] = False
            root_ds, pred = sparse.csgraph.dijkstra(mesh.csgraph,
                                                    False,
                                                    root,
                                                    return_predecessors=True)
    if root is None:
        # there is no soma close, so use far point heuristic
        root, target, pred, dm, root_ds = find_far_points(mesh)
        valid[root] = 0

    return root, root_ds, pred, valid


def paths_to_edges(path_list):
    arrays = []
    for path in path_list:
        p = np.array(path)
        e = np.vstack((p[0:-1], p[1:])).T
        arrays.append(e)
    return np.vstack(arrays)


def smooth_graph(verts, edges, neighborhood=2, iterations=100, r=.1):
    """ smooths a spatial graph via iterative local averaging
        calculates the average position of neighboring vertices
        and relaxes the vertices toward that average

        :param verts: a NxK numpy array of vertex positions
        :param edges: a Mx2 numpy array of vertex indices that are edges
        :param neighborhood: an integer of how far in the graph to relax over
        as being local to any vertex (default = 2)
        :param iterations: number of relaxation iterations (default = 100)
        :param r: relaxation factor at each iteration
        new_vertex = (1-r)*old_vertex + r*(local_avg)

        :return: new_verts
        verts is a Nx3 list of new smoothed vertex positions

    """
    N = len(verts)
    E = len(edges)
    # setup a sparse matrix with the edges
    sm = sparse.csc_matrix(
        (np.ones(E), (edges[:, 0], edges[:, 1])), shape=(N, N))

    # an identity matrix
    eye = sparse.csc_matrix((np.ones(N, dtype=np.float32),
                                    (np.arange(0, N), np.arange(0, N))),
                                    shape=(N, N))
    # for undirected graphs we want it symettric
    sm = sm + sm.T

    # this will store our relaxation matrix
    C = sparse.csc_matrix(eye)
    # multiple the matrix and add to itself
    # to spread connectivity along the graph
    for i in range(neighborhood):
        C = C + sm @ C
    # zero out the diagonal elements
    C.setdiag(np.zeros(N))
    # don't overweight things that are connected in more than one way
    C = C.sign()
    # measure total effective neighbors per node
    neighbors = np.sum(C, axis=1)

    # normalize the weights of neighbors according to number of neighbors
    neighbors = 1/neighbors
    C = C.multiply(neighbors)
    # convert back to csc
    C = C.tocsc()

    # multiply weights by relaxation term
    C *= r

    # construct relaxation matrix, adding on identity with complementary weight
    A = C + (1-r)*eye

    # make a copy of original vertices to no destroy inpuyt
    new_verts = np.copy(verts)

    # iteratively relax the vertices
    for i in range(iterations):
        new_verts = A*new_verts
    return new_verts

def collapse_soma_skeleton(soma_pos, verts, edges, soma_d_thresh=12000):
    soma_pos_m = soma_pos[np.newaxis, :]
    dv = np.linalg.norm(verts - soma_pos_m, axis=1)
    soma_verts = np.where(dv < soma_d_thresh)[0]
    new_verts = np.vstack((verts, soma_pos_m))
    soma_i = verts.shape[0]
    edges[np.isin(edges, soma_verts)] = soma_i
    simple_verts, simple_edges = trimesh_vtk.remove_unused_verts(new_verts, edges)
    good_edges = ~(simple_edges[:, 0] == simple_edges[:, 1])
    return simple_verts, simple_edges[good_edges]