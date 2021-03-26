import random
import torch
import time
from sklearn.neighbors import NearestNeighbors

__all__ = ['mat_intra_cam_loss', 'mat_inter_cam_loss']

def proxy_loss(features, abs_proxy_labels, memory, temp=0.07):
    '''loss for debugging'''
    stored_proxies = memory.storage.clone().detach()
    sim = torch.mm(features, stored_proxies.t()) / temp
    return torch.nn.functional.cross_entropy(sim, abs_proxy_labels)

def _proxy_index_mapping(proxy_set, proxy_labels):
    mapping = {}
    for i, plabel in enumerate(proxy_set):
        mapping[plabel] = i

    return torch.tensor([mapping[i] for i in proxy_labels.tolist()]).to(proxy_labels.device)

def mat_intra_cam_loss(features, camids, abs_proxy_labels, memory, temp=0.07):
    '''
    Compute intra-camera loss.

    Args:
        features: tensor, extracted features from the backbone model.
        camids: list, list of camera indices in current batch.
        abs_proxy_labels: tensor, absolute proxy labels in currenty batch.
        memory: memory bank.
        temp: float, temperature factor, default 0.07.

    Return:
        Intra-camera loss.
    '''
    # import ipdb; ipdb.set_trace()
    camids = torch.tensor([int(str_id.split('_')[-1]) for str_id in camids])
    cam_set = set(camids.tolist())

    intra_loss = torch.tensor(0, dtype=torch.float).to(features.device)

    for cam_name in cam_set:
        indices = torch.where(cam_name==camids)[0]
        # if torch.cuda.is_available():
        #     indices = indices.cuda()


        # find proxies
        proxies_per_cam = []
        proxy_set = sorted(set(abs_proxy_labels[indices].cpu().tolist()))
        for proxy in proxy_set:
            proxies_per_cam.append(memory.storage[proxy].clone().view(1,-1))
        proxies_per_cam = torch.cat(proxies_per_cam, dim=0)

        # 求一个相机下的loss
        # Step 1: 找到当前相机下的feature
        features_per_cam = features[indices]

        # Step 2: 求当前相机下的损loss
        targets = _proxy_index_mapping(proxy_set, abs_proxy_labels[indices]) # 为满足nll_loss要求，进行序号转换

        # using torch.nn.functional.softmax
        sim_per_cam = torch.mm(features_per_cam, proxies_per_cam.t())
        sim_per_cam = torch.div(sim_per_cam, temp)
        intra_loss_per_cam = torch.nn.functional.cross_entropy(sim_per_cam, targets)

        # Step 3: accumulate loss
        intra_loss = intra_loss + intra_loss_per_cam
    
    # return torch.div(intra_loss, len(cam_set))
    return intra_loss

def mat_inter_cam_loss(k, features, camids, cluster_labels, all_labels, memory, temp=0.07, size_reduce=False):
    '''
    Compute inter-camera loss.

    Args:
        k: int, k parameter of KNN hard negative mining.
        features: tensor, extracted features from the backbone model.
        camdis: list, list of camera indices in current batch.
        cluster_labels: tensor, pseudo cluster labels generated by global clustering.
        all_labels: dict, labels of all training samples, the output of get_all_sample_labels().
        memory: memory bank.
        temp: float, temperature factor, default 0.07.

    Returns:
        Inter-camera loss.
    '''

    bsize = features.size(0)
    inter_loss = torch.tensor(0, dtype=torch.float).to(features.device)
    

    for i in range(bsize):
        feat = features[i].clone().view(1,-1)

        # start_time = time.time()
        pos_proxies, abs_pos_proxy_labels = _retrieve_all_positive_proxies(feat.view(1,-1), cluster_labels[i], camids[i], all_labels, memory)
        # print('positive proxy retrival in {:.3f}s'.format(time.time()-start_time))
        
        # start_time = time.time()
        neg_proxies, abs_neg_proxy_labels = _retrieve_k_nearest_negative_proxies(k, feat.view(1,-1), cluster_labels[i], all_labels, memory, size_reduce=size_reduce)
        # print('negative proxy retrival in {:.3f}s'.format(time.time()-start_time))
        
        if pos_proxies is None:
            continue
        

        pos_card = abs_pos_proxy_labels.size(0)


        # 手动求log softmax
        # import ipdb; ipdb.set_trace()
        pos_sim = torch.exp(torch.div(torch.mm(feat.view(1,-1), pos_proxies.t()), temp))
        neg_sim = torch.exp(torch.div(torch.mm(feat.view(1,-1), neg_proxies.t()), temp))
        norm_deno = torch.sum(pos_sim) + torch.sum(neg_sim)

        # NLL loss
        inter_loss = inter_loss - torch.div(torch.sum(torch.log(torch.div(pos_sim, norm_deno))), pos_card)


    # return torch.div(inter_loss, bsize)
    return inter_loss


def _retrieve_all_positive_proxies(features, cluster_labels, camids, all_labels, memory):
    bsize = features.size(0)
    res = []
    res_labels = []

    for i in range(bsize):
        cls_label = cluster_labels.view(-1)[i]
        indices = torch.where(all_labels['cluster']==cls_label.cpu())[0].tolist() # same cluster
        indices = torch.tensor([idx for idx in indices if all_labels['camid'][idx] != camids]) # different camera

        if indices.size(0) == 0:
            continue

        pos_proxy_indices = sorted(set(all_labels['abs_proxy'][indices].tolist()))
        res.append(memory.storage[torch.tensor(pos_proxy_indices)]) # positive centroids
        res_labels.append(torch.tensor(pos_proxy_indices))

    if len(res) == 0:
        return None, None
    
    return torch.cat(res, dim=0).to(features.device), torch.cat(res_labels).to(features.device)


def _retrieve_k_nearest_negative_proxies(k, features, cluster_labels, all_labels, memory, size_reduce=False):
    bsize = features.size(0)
    res = []
    res_labels = []


    for i in range(bsize):
        cls_label = cluster_labels.view(-1)[i]
        indices = torch.where(all_labels['cluster']!=cls_label.cpu())[0]
        
        neg_proxy_indices = sorted(set(all_labels['abs_proxy'][indices].tolist()))

        # reduce neg proxy size, speed up training
        if size_reduce:
            size = 100
            neg_proxy_indices = sorted(random.sample(neg_proxy_indices, size))


        neg_proxy_centroids = memory.storage[torch.tensor(neg_proxy_indices)]
            

        # nearest_features, nearest_indices = _sklearn_knn(k, features[i].view(1,-1), neg_proxy_centroids, neg_proxy_indices) # using sklearn
        nearest_features, nearest_indices = _sort_knn(k, features[i].view(1,-1), neg_proxy_centroids, neg_proxy_indices) # using sorting

        res.append(nearest_features)
        res_labels.append(nearest_indices)

    return torch.cat(res, dim=0).to(features.device), torch.cat(res_labels).to(features.device)

def _sort_knn(k, x, features, input_indices):
    input_indices = torch.tensor(input_indices)
    diff_norm = torch.norm(features-x, dim=1)
    _, indices = torch.sort(diff_norm)
    return features[indices[:k]], input_indices[indices[:k]]


def _sklearn_knn(k, x, features, input_indices):
    print('knn features shape: {}'.format(features.size()))

    input_indices = torch.tensor(input_indices)
    neigh = NearestNeighbors(n_neighbors=k, n_jobs=-1)

    # start_time = time.time()

    neigh.fit(features.detach().cpu().numpy())

    # print('knn time consume: {:.3f}s'.format(time.time()-start_time))

    _, indices = neigh.kneighbors(x.detach().cpu().numpy())
    return features[torch.tensor(indices).view(-1)], input_indices[torch.tensor(indices).view(-1)]