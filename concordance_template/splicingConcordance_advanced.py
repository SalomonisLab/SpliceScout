import math
import sys,string,os
sys.path.insert(1, os.path.join(sys.path[0], '..')) ### import parent dir dependencies
import export
import copy
import UI

def cleanUpLine(line):
    line = string.replace(line,'\n','')
    line = string.replace(line,'\c','')
    data = string.replace(line,'\r','')
    data = string.replace(data,'"','')
    return data

def filepath(filename):
    fn = unique.filepath(filename)
    return fn
          
def compareEventLists(PSI_ref_dir,PSI_query_dir,minimumOverlap=10,dPSI=None,rawp=None):
    import collections
    psi_filename={}
    event_db = collections.OrderedDict()
    event_db_query = collections.OrderedDict()
    groups_list=['']
    groups_list_query=['']
    files2 = UI.read_directory(PSI_query_dir)
    for file in files2:
        psi_filename[file]=PSI_query_dir
    files = UI.read_directory(PSI_ref_dir)
    for file in files:
        psi_filename[file]=PSI_ref_dir
    files = files2 + files
    file_headers = {}
    for file in files:
        if '.txt' in file and 'PSI.' in file or '__' in file or '_significant-results.tsv' in file:
            ls={}
            if file in files2:
                event_db_query[file[:-4]]=ls
                groups_list_query.append(file[:-4])
            else:
                event_db[file[:-4]]=ls
                groups_list.append(file[:-4])
            folder = psi_filename[file]
            fn = folder+'/'+file
            firstLine = True
            for line in open(fn,'rU').xreadlines():
                data = line.rstrip()
                t = string.split(data,'\t')
                if firstLine:
                    file_headers[file[:-4]] = t ### Store the headers
                    ### GUARD: a query file whose header is missing an expected column made t.index() throw an
                    ### UNCAUGHT ValueError that crashed the WHOLE concordance run (a blocker). Skip the one
                    ### malformed file with a warning and keep scoring the rest.
                    try:
                        if "Feature" in t:
                            ui = t.index('Feature')
                            gsi = t.index('Gene_Symbol')
                            eai = t.index('Event_Type')
                            dpi = None
                        else:
                            ui = 0
                            gsi = None
                            cid = t.index('ClusterID')
                            eai = t.index('EventAnnotation')
                            dpi = t.index('dPSI')
                            rpi = t.index('rawp')
                    except ValueError, e:
                        print 'WARNING: skipping', file, '-- header missing an expected column (', e, '); fields=', len(t)
                        break
                    firstLine= False
                    continue
                else:
                    if dpi == None:
                        uid = t[gsi]+":"+t[ui]
                        uid = string.split(uid,'|')[0]
                        event_annot = t[eai]
                        raw_dPSI_value = float(t[15])
                    else:
                        uid = t[0]
                        uid = string.split(uid,'|')[0]
                        event_annot = t[eai]
                        raw_dPSI_value = float(t[dpi])
                        dPSI_value = abs(float(t[dpi]))
                        rawp_value = float(t[rpi])
                        if dPSI != None:
                            if dPSI_value<dPSI:
                                continue
                        if rawp != None:
                            if rawp_value>rawp:
                                continue
                    if raw_dPSI_value < 0:
                        direction = 'exclusion'
                    else:
                        direction = 'inclusion'
                    #print [uid,event_annot,raw_dPSI_value];sys.exit()
                    if RemoveIR:
                        if 'intron' not in event_annot:
                            ls[(uid,direction)]=t ### Keep the event data for output

                    else:
                        ls[(uid,direction)]=t ### Keep the event data for output

    def convertEvents(events):
        opposite_events=[]
        for (event,direction) in events:
            if direction == 'exclusion':
                direction = 'inclusion'
            else:
                direction = 'exclusion'
            opposite_events.append((event,direction))
        return opposite_events
        
    ea1 = export.ExportFile(folder+'/overlaps-same-direction.txt')
    ea2 = export.ExportFile(folder+'/overlaps-opposite-direction.txt')
    ea3 = export.ExportFile(folder+'/concordance.txt')
    #ea4 = export.ExportFile(folder+'/overlap-same-direction-events.txt')
    ea1.write(string.join(groups_list_query,'\t')+'\n')
    ea2.write(string.join(groups_list_query,'\t')+'\n')
    ea3.write(string.join(groups_list_query,'\t')+'\n')
    ### SpliceScout: long-form per-pair counts for the ANALYTIC NULL (score_with_null.py). For every drug x subtype
    ### pair with any shared event: n = |E| (same + opposite direction) and the inclusion counts of each signature
    ### restricted to E (-> p_P, p_D -> pi0). concordance.txt above is unchanged (same numbers, same floor).
    ps = open(folder+'/pair_stats.tsv','w')
    ps.write('drug_signature\tsubtype_signature\tn_overlap\tsame\topposite\tdrug_inclusion\tsubtype_inclusion\n')

    comparison_db={}
    best_hits={}
    print len(event_db_query),len(event_db)
    for comparison1 in event_db:
        events1 = event_db[comparison1]
        #print comparison1, len(events1), events1
        hits1=[comparison1]
        hits2=[comparison1]
        hits3=[comparison1]
        best_hits[comparison1]=[]
        for comparison2 in event_db_query:
            events2 = event_db_query[comparison2]
            #print comparison2, len(events2), events2[:5];sys.exit()
            events3 = convertEvents(events2)
            overlapping_events = list(set(events1).intersection(events2))
            overlap = len(overlapping_events)
            opposite_events = set(events1).intersection(events3) ### drug-side (uid,dir) whose subtype dir is flipped
            inverse_overlap = len(opposite_events) ### Get opposite events
            if overlap+inverse_overlap > 0:
                inc_same = len([1 for (u,d) in overlapping_events if d == 'inclusion'])
                inc_opp = len([1 for (u,d) in opposite_events if d == 'inclusion'])
                drug_inc = inc_same + inc_opp                             ### drug inclusion within E
                sub_inc = inc_same + (inverse_overlap - inc_opp)          ### subtype inclusion within E
                ps.write(string.join([comparison1, comparison2, str(overlap+inverse_overlap), str(overlap),
                                      str(inverse_overlap), str(drug_inc), str(sub_inc)],'\t')+'\n')
            ### Calculate ratios based on the size of the smaller set
            min_events1 = min([len(events1),len(events2)]) 
            min_events2 = min([len(events1),len(events3)])
            denom = overlap+inverse_overlap
            if denom == 0: denom = 0.00001
            #comparison_db[comparison1,comparison2]=overlap
            if min_events1 == 0: min_events1 = 1
            if (overlap+inverse_overlap)<minimumOverlap:
                hits1.append('0.5')
                hits2.append('0.5')
                hits3.append('0.5|0.5')
            else:
                hits1.append(str((1.00*overlap)/min_events1))
                hits2.append(str((1.00*inverse_overlap)/min_events1))
                hits3.append(str(1.00*overlap/denom)+'|'+str(1.00*inverse_overlap/denom)+':'+str(overlap+inverse_overlap))
                if 'Leu' not in comparison2:
                    comp_name = string.split(comparison2,'_vs')[0]
                    best_hits[comparison1].append([abs(1.00*overlap/denom),'cor',comp_name])
                    best_hits[comparison1].append([abs(1.00*inverse_overlap/denom),'anti',comp_name])
            if comparison1 != comparison2:
                if len(overlapping_events)>0:
                    #ea4.write(string.join(['UID',comparison1]+file_headers[comparison1]+[comparison2]+file_headers[comparison2],'\t')+'\n')
                    pass
                overlapping_events.sort()
                for event in overlapping_events:
                    vals = string.join([event[0],comparison1]+event_db[comparison1][event]+[comparison2]+event_db_query[comparison2][event],'\t')
                    #ea4.write(vals+'\n')
                    pass
        ea1.write(string.join(hits1,'\t')+'\n')
        ea2.write(string.join(hits2,'\t')+'\n')
        ea3.write(string.join(hits3,'\t')+'\n')
    ea1.close()
    ea2.close()
    ea3.close()
    ps.close()
    #ea4.close()
    for comparison in best_hits:
        best_hits[comparison].sort()
        best_hits[comparison].reverse()
        hits = best_hits[comparison][:10]
        hits2=[]
        for (score,dir,comp) in hits:
            h = str(score)[:4]+'|'+dir+'|'+comp
            hits2.append(h)
        print comparison,'\t',string.join(hits2,', ')
     
if __name__ == '__main__':
    import getopt
    species = 'Hs'
    degrees = 'direct'
    RemoveIR = False
    rawp = None
    dPSI = None
    if len(sys.argv[1:])<=1:  ### Indicates that there are insufficient number of command-line arguments
        print "Insufficient options provided";sys.exit()
    else:
        options, remainder = getopt.getopt(sys.argv[1:],'', ['PSI_ref_dir=','PSI_query_dir=','rawp=', 'dPSI=', 
                                    'minimumOverlap=','r=','q=','ref=','query=','removeIR='])
        for opt, arg in options:
            if opt == '--ref': PSI_ref_dir=arg
            elif opt == '--query': PSI_query_dir=arg
            elif opt == '--dPSI': dPSI=float(arg)
            elif opt == '--rawp': rawp=float(arg)
            elif opt == '--minimumOverlap': minimumOverlap = arg
            elif opt == '--removeIR':
                if arg == 'True':
                    RemoveIR = True

    print "Filtering based on a dPSI cutoff:",dPSI,"and a rawp cutoff:",rawp

    compareEventLists(PSI_ref_dir,PSI_query_dir,minimumOverlap=5,dPSI=dPSI,rawp=rawp);sys.exit()