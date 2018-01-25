import argparse
import sys
import os
import os.path as op
from glob import glob
import numpy as np
from nipype import Workflow, Node, MapNode, DataSink
from nipype.interfaces import fsl, freesurfer as fs
import json


def main(arglist):
    """Preprocess NYU CBI Prisma data"""

    # Parse arguments
    parser = argparse.ArgumentParser()
    parser.add_argument('-subject', required=True,
                        help=('Freesurfer subject id. Note that we use the $SUBJECTS_DIR '
                              'environmental variable to find the required data'))
    parser.add_argument('-datadir', required=True, help='Raw MR data path')
    parser.add_argument('-outdir', required=True, help='Output directory path')
    parser.add_argument('-epis', required=True, nargs='+', type=int,
                        help='EPI scan numbers')
    parser.add_argument('-sbref', required=True, type=int,
                        help='Single band reference scan number')
    parser.add_argument('-distortPE', required=True,
                        help=('Distortion scan number with same PE as epis. Should be number if '
                              'dir_structure is prisma, and two letter string (e.g., AP, PA) if '
                              'dir_structure is bids'))
    parser.add_argument('-distortrevPE', required=True,
                        help=('Distortion scan number with reverse PE as epis. Should be number if'
                              ' dir_structure is prisma, and two letter string (e.g., AP, PA) if '
                              'dir_structure is bids'))
    parser.add_argument('-PEdim', type=str, default='y', 
                        help='PE dimension (x, y, or z)')
    parser.add_argument("-plugin", type=str, default="MultiProc",
                        help=("Nipype plugin to use for running this. MultiProc (default) is "
                              "normally fine for when running locally, though it may use up all "
                              "your  computer's resources. Linear is slower, but won't do that."
                              "SLURM should be used on NYU HPC prince cluster."))
    parser.add_argument('-working_dir', default=None,
                        help=("Path to your working directory. By default, this will be within your"
                              "output directory, but you may want to place it elsewhere. For "
                              "example, on the HPC cluster, you may run out of space if this is in"
                              "your /home directory, so you probably want this in /scratch"))
    parser.add_argument('-dir_structure', default='prisma',
                        help=("{prisma, bids}. Is your data directory structured like it just came"
                              " off the prisma scanner ('prisma') or is it BIDS structured "
                              "('BIDS')? This determines how we look for the various scans. If "
                              "your data is BIDS-structured, then datadir should point to the "
                              "particular session you want to preprocess as well"))
    parser.add_argument('-plugin_args', default=None,
                        help=("Any additional arguments to pass to nipype's workflow.run as plugin"
                              "_args. A single entry in the resulting dictionary should be of the"
                              " format arg:val (e.g., n_procs:2) with multiple args separated by a"
                              " comma with no spaces (e.g., n_procs:2,memory_gb:5). see nipype's "
                              "plugin documentation for more details on possible values: "
                              "http://nipype.readthedocs.io/en/latest/users/plugins.html. "))
    args = vars(parser.parse_args(arglist))

    # Session paths and files
    session = dict()
    session['subj'] = args['subject']
    session['data'] = args['datadir']
    if args['dir_structure'] == 'prisma':
        session['nii_temp'] = op.join(session['data'], '%02d+*', '*.nii')
        session['epis'] = [glob(session['nii_temp'] %r)[0] for r in args['epis']]
        session['sbref'] = glob(session['nii_temp'] %args['sbref'])[0]
        # this is a bit of a hack. for the bids structure, these two values will be strings and so
        # we can't set type=int in the argparse arguments above. however, we do want to use %02d
        # formatting string, so we'll just cast these two as ints here.
        session['distort_PE'] = glob(session['nii_temp'] %int(args['distortPE']))[0]
        session['distort_revPE'] = glob(session['nii_temp'] %int(args['distortrevPE']))[0]
    elif args['dir_structure'] == 'bids':
        session['nii_temp'] = op.join(session['data'], '%s', '*-%02d_%s.nii')
        session['nii_fmap_temp'] = op.join(session['data'], '%s', '*-%s_%s.nii')
        session['epis'] = [glob(session['nii_temp'] % ('func', r, 'bold'))[0] for r in args['epis']]
        session['sbref'] = glob(session['nii_temp'] % ('func', args['sbref'], 'sbref'))[0]
        session['distort_PE'] = glob(session['nii_fmap_temp'] % ('fmap', args['distortPE'], 'epi'))[0]
        session['distort_revPE'] = glob(session['nii_fmap_temp'] % ('fmap', args['distortrevPE'], 'epi'))[0]
    else:
        raise Exception("Don't know what to do with dir_structure %s!" % args['dir_structure'])
    session['PE_dim'] = args['PEdim']
                          
    # Preproc output directory
    session['out'] = args['outdir']
    if not op.exists(session['out']):
        os.makedirs(session['out'])

    if args['working_dir'] is not None:
        session['working_dir'] = args['working_dir']
    else:
        session['working_dir'] = session['out']
    if not op.exists(session["working_dir"]):
        os.makedirs(session['working_dir'])

    session['plugin_args'] = {}
    if args['plugin_args'] is not None:
        for arg in args['plugin_args'].split(','):
            if len(arg.split(':')) != 2:
                raise Exception("Your plugin_args is incorrectly formatted, each should contain one colon!")
            k, v = arg.split(':')
            try:
                session['plugin_args'][k] = int(v)
            except ValueError:
                try:
                    session['plugin_args'][k] = float(v)
                except ValueError:
                    session['plugin_args'][k] = v
    session['plugin'] = args['plugin']

    # Dump session info to json
    with open(op.join(session['out'], 'session.json'), 'w') as sess_file:
        json.dump(session, sess_file, sort_keys=True, indent=4)

    # Define preprocessing worklow
    preproc_wf = create_preproc_workflow(session)

    # Execute workflow in parallel
    preproc_wf.run(session["plugin"], plugin_args=session['plugin_args'])


def create_preproc_workflow(session):
    """
    Defines simple functional preprocessing workflow, including motion
    correction, registration to distortion scans, and unwarping. Assumes
    recon-all has been performed on T1, and computes but does not apply
    registration to anatomy.
    """

    #---Create workflow---
    wf = Workflow(name='workflow', base_dir=session['working_dir'])


    #---EPI Realignment---

    # Realign every TR in each functional run to the sbref image using mcflirt
    realign = MapNode(fsl.MCFLIRT(ref_file=session['sbref'],
                                  save_mats=True,
                                  save_plots=True),
                      iterfield=['in_file'], name='realign')
    realign.inputs.in_file = session['epis']
    wf.add_nodes([realign])


    #---Registration to distortion scan---

    # Register the sbref scan to the distortion scan with the same PE using flirt
    reg2dist = Node(fsl.FLIRT(in_file=session['sbref'],
                              reference=session['distort_PE'],
                              out_file='sbref_reg.nii.gz',
                              out_matrix_file='sbref2dist.mat',
                              dof=6),
                    name='reg2distort')
    wf.add_nodes([reg2dist])


    #---Distortion correction---

    # Merge the two distortion scans for unwarping
    distort_scans = [session['distort_PE'], session['distort_revPE']]
    merge_dist = Node(fsl.Merge(in_files=distort_scans,
                                dimension='t',
                                merged_file='distortion_merged.nii.gz'),
                      name='merge_distort')
    wf.add_nodes([merge_dist])

    # Run topup to estimate warpfield and create unwarped distortion scans
    PEs = np.repeat([session['PE_dim'], session['PE_dim'] + '-'], 3)
    unwarp_dist = Node(fsl.TOPUP(encoding_direction=list(PEs),
                                 readout_times=[1, 1, 1, 1, 1, 1],
                                 config='b02b0.cnf'),
                       name='unwarp_distort')
    wf.connect(merge_dist, 'merged_file', unwarp_dist, 'in_file')

    # Unwarp sbref image in case it's useful
    unwarp_sbref = Node(fsl.ApplyTOPUP(in_index=[1], method='jac'),
                        name='unwarp_sbref')
    wf.connect([(reg2dist, unwarp_sbref,
                 [('out_file', 'in_files')]),
                (unwarp_dist, unwarp_sbref,
                    [('out_enc_file', 'encoding_file'),
                     ('out_fieldcoef', 'in_topup_fieldcoef'),
                     ('out_movpar', 'in_topup_movpar')])])


    #---Registration to anatomy---

    # Create mean unwarped distortion scan
    mean_unwarped_dist = Node(fsl.MeanImage(dimension='T'),
                              name='mean_unwarped_distort')
    wf.connect(unwarp_dist, 'out_corrected', mean_unwarped_dist, 'in_file')

    # Register mean unwarped distortion scan to anatomy using bbregister
    reg2anat = Node(fs.BBRegister(subject_id=session['subj'],
                                  contrast_type='t2',
                                  init='fsl',
                                  out_reg_file='distort2anat_tkreg.dat',
                                  out_fsl_file='distort2anat_flirt.mat'),
                    name='reg2anat')
    wf.connect(mean_unwarped_dist, 'out_file', reg2anat, 'source_file')


    #---Combine and apply transforms to EPIs---

    # Split EPI runs into 3D files
    split_epis = MapNode(fsl.Split(dimension='t'),
                         iterfield=['in_file'], name='split_epis')
    split_epis.inputs.in_file = session['epis']
    wf.add_nodes([split_epis])

    # Combine the rigid transforms to be applied to each EPI volume
    concat_rigids = MapNode(fsl.ConvertXFM(concat_xfm=True),
                            iterfield=['in_file'],
                            nested=True,
                            name='concat_rigids')
    wf.connect([(realign, concat_rigids,
                 [('mat_file', 'in_file')]),
                (reg2dist, concat_rigids,
                    [('out_matrix_file', 'in_file2')])])

    # Apply rigid transforms and warpfield to each EPI volume
    correct_epis = MapNode(fsl.ApplyWarp(interp='spline', relwarp=True),
                           iterfield=['in_file', 'ref_file', 'premat'],
                           nested=True,
                           name='correct_epis')

    get_warp = lambda warpfields: warpfields[0]
    wf.connect([(split_epis, correct_epis,
                    [('out_files', 'in_file'),
                     ('out_files', 'ref_file')]),
                (concat_rigids, correct_epis,
                    [('out_file', 'premat')]),
                (unwarp_dist, correct_epis,
                    [(('out_warps', get_warp), 'field_file')])])

    # Merge processed files back into 4D nifti
    merge_epis = MapNode(fsl.Merge(dimension='t',
                                   merged_file='timeseries_corrected.nii.gz'),
                         iterfield='in_files',
                         name='merge_epis')
    wf.connect([(correct_epis, merge_epis, [('out_file', 'in_files')])])


    #---Copy important files to main directory---
    substitutions = [('_merge_epis%d/timeseries_corrected.nii.gz' % r,
                      'timeseries_corrected_run%02d.nii.gz' % (r+1))
                     for r in np.arange(len(session['epis']))]
    ds = Node(DataSink(base_directory=os.path.abspath(session['out']),
                       substitutions=substitutions),
              name='outfiles')
    wf.connect(unwarp_dist, 'out_corrected', ds, '@unwarp_dist')
    wf.connect(mean_unwarped_dist, 'out_file', ds, '@mean_unwarped_dist')
    wf.connect(unwarp_sbref, 'out_corrected', ds, '@unwarp_sbref')
    wf.connect(reg2anat, 'out_reg_file', ds, '@reg2anat')
    wf.connect(merge_epis, 'merged_file', ds, '@merge_epis')

    return wf


if __name__ == '__main__':
    main(sys.argv[1:])
