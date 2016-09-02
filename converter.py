from __future__ import print_function

import math
import base64
import struct

from panda3d.core import *
from panda3d import bullet

class Converter():
    def __init__(self):
        self.cameras = {}
        self.lights = {}
        self.textures = {}
        self.mat_states = {}
        self.mat_mesh_map = {}
        self.meshes = {}
        self.nodes = {}
        self.scenes = {}
        self.characters = {}

        # Scene props
        self.active_scene = NodePath(ModelRoot('default'))
        self.background_color = (0, 0, 0)
        self.active_camera = None

    def update(self, gltf_data, writing_bam=False):
        # Convert data
        for camname, gltf_cam in gltf_data.get('cameras', {}).items():
            self.load_camera(camname, gltf_cam)

        if 'extras' in gltf_data:
            for lightname, gltf_light in gltf_data['extras'].get('lights', {}).items():
                self.load_light(lightname, gltf_light)

        for texname, gltf_tex in gltf_data.get('textures', {}).items():
            self.load_texture(texname, gltf_tex, gltf_data)

        for matname, gltf_mat in gltf_data.get('materials', {}).items():
            self.load_material(matname, gltf_mat)

        for meshname, gltf_mesh in gltf_data.get('meshes', {}).items():
            self.load_mesh(meshname, gltf_mesh, gltf_data)

        for nodename, gltf_node in gltf_data.get('nodes', {}).items():
            node = self.nodes.get(nodename, PandaNode(nodename))
            matrix = self.load_matrix(gltf_node['matrix'])
            node.set_transform(TransformState.make_mat(matrix))
            self.nodes[nodename] = node

        # If we support writing bam 6.40, we can safely write out
        # instanced lights.  If not, we have to copy it.
        copy_lights = writing_bam and not hasattr(BamWriter, 'root_node')

        # Build scenegraphs
        def add_node(root, gltf_scene, nodeid):
            if nodeid not in gltf_data['nodes']:
                print("Could not find node with id: {}".format(nodeid))
                return

            gltf_node = gltf_data['nodes'][nodeid]
            if 'jointName' in gltf_node:
                # don't handle joints here
                return
            panda_node = self.nodes[nodeid]

            if 'extras' in gltf_scene and 'hidden_nodes' in gltf_scene['extras']:
                if nodeid in gltf_scene['extras']['hidden_nodes']:
                    panda_node = panda_node.make_copy()
            np = root.attach_new_node(panda_node)

            if 'meshes' in gltf_node:
                np_tmp = np

                if 'skeletons' in gltf_node:
                    char = self.characters[nodeid]
                    np_tmp = np.attach_new_node(char)

                for meshid in gltf_node['meshes']:
                    mesh = self.meshes[meshid]
                    np_tmp.attach_new_node(mesh)
            if 'camera' in gltf_node:
                camid = gltf_node['camera']
                cam = self.cameras[camid]
                np.attach_new_node(cam)
            if 'extras' in gltf_node:
                if 'light' in gltf_node['extras']:
                    lightid = gltf_node['extras']['light']
                    light = self.lights[lightid]
                    if copy_lights:
                        light = light.make_copy()
                    lnp = np.attach_new_node(light)
                    if isinstance(light, Light):
                        root.set_light(lnp)
            if 'extensions' in gltf_node:
                if 'BLENDER_physics' in gltf_node['extensions']:
                    phy = gltf_node['extensions']['BLENDER_physics']
                    shape = None
                    radius = max(phy['dimensions'][0], phy['dimensions'][1]) / 2.0
                    height = phy['dimensions'][2]
                    geomnode = None
                    if 'mesh' in phy:
                        try:
                            geomnode = self.meshes[phy['mesh']]
                        except KeyError:
                            print("Could not find physics mesh ({}) for object ({})".format(phy['mesh'], nodeid))

                    if phy['collision_shape'] == 'box':
                        shape = bullet.BulletBoxShape(LVector3(*phy['dimensions']) / 2.0)
                    elif phy['collision_shape'] == 'sphere':
                        shape = bullet.BulletSphereShape(max(phy['dimensions']) / 2.0)
                    elif phy['collision_shape'] == 'capsule':
                        shape = bullet.BulletCapsuleShape(radius, height - 2.0 * radius, bullet.ZUp)
                    elif phy['collision_shape'] == 'cylinder':
                        shape = bullet.BulletCylinderShape(radius, height, bullet.ZUp)
                    elif phy['collision_shape'] == 'cone':
                        shape = bullet.BulletConeShape(radius, height, bullet.ZUp)
                    elif phy['collision_shape'] == 'convex_hull':
                        if geomnode:
                            shape = bullet.BulletConvexHullShape()

                            for geom in geomnode.get_geoms():
                                shape.add_geom(geom)
                    elif phy['collision_shape'] == 'mesh':
                        if geomnode:
                            mesh = bullet.BulletTriangleMesh()
                            for geom in geomnode.get_geoms():
                                mesh.add_geom(geom)
                            shape = bullet.BulletTriangleMeshShape(mesh, dynamic=phy['dynamic'])
                    else:
                        print("Unknown collision shape ({}) for object ({})".format(phy['collision_shape'], nodeid))

                    if shape is not None:
                        phynode = bullet.BulletRigidBodyNode(gltf_node['name'])
                        phynode.add_shape(shape)
                        np.attach_new_node(phynode)
                        if phy['dynamic']:
                            phynode.set_mass(phy['mass'])
                    else:
                        print("Could not create collision shape for object ({})".format(nodeid))


            for child_nodeid in gltf_node['children']:
                add_node(np, gltf_scene, child_nodeid)

            # Handle visibility after children are loaded
            def visible_recursive(node, visible):
                if visible:
                    node.show()
                else:
                    node.hide()
                for child in node.get_children():
                    visible_recursive(child, visible)
            if 'extras' in gltf_scene and 'hidden_nodes' in gltf_scene['extras']:
                if nodeid in gltf_scene['extras']['hidden_nodes']:
                    #print('Hiding', np)
                    visible_recursive(np, False)
                else:
                    #print('Showing', np)
                    visible_recursive(np, True)

            # Check if we need to deal with negative scale values
            scale = panda_node.get_transform().get_scale()
            negscale = scale.x * scale.y * scale.z < 0
            if (negscale):
                for geomnode in np.find_all_matches('**/+GeomNode'):
                    tmp = geomnode.get_parent().attach_new_node(PandaNode('ReverseCulling'))
                    tmp.set_attrib(CullFaceAttrib.make_reverse())
                    geomnode.reparent_to(tmp)

        for scenename, gltf_scene in gltf_data.get('scenes', {}).items():
            scene_root = NodePath(ModelRoot(scenename))

            for nodeid in gltf_scene['nodes']:
                add_node(scene_root, gltf_scene,  nodeid)

            self.scenes[scenename] = scene_root

        # Set the active scene
        sceneid = gltf_data['scene']
        self.active_scene = self.scenes[sceneid]
        if 'scenes' in gltf_data:
            gltf_scene = gltf_data['scenes'][sceneid]
            if 'extras' in gltf_scene:
                if 'background_color' in gltf_scene['extras']:
                    self.background_color = gltf_scene['extras']['background_color']
                if 'active_camera' in gltf_scene['extras']:
                    self.active_camera = gltf_scene['extras']['active_camera']

    def load_matrix(self, mat):
        lmat = LMatrix4()

        for i in range(4):
            lmat.set_row(i, LVecBase4(*mat[i * 4: i * 4 + 4]))
        return lmat

    def load_texture(self, texname, gltf_tex, gltf_data):
        source = gltf_data['images'][gltf_tex['source']]
        uri = Filename.fromOsSpecific(source['uri'])
        texture = TexturePool.load_texture(uri, 0, False, LoaderOptions())
        use_srgb = False
        if 'format' in gltf_tex and gltf_tex['format'] in (0x8C40, 0x8C42):
            use_srgb = True
        elif 'internalFormat' in gltf_tex and gltf_tex['internalFormat'] in (0x8C40, 0x8C42):
            use_srgb = True

        if use_srgb:
            if texture.get_num_components() == 3:
                texture.set_format(Texture.F_srgb)
            elif texture.get_num_components() == 4:
                texture.set_format(Texture.F_srgb_alpha)
        self.textures[texname] = texture

    def load_material(self, matname, gltf_mat):
        state = self.mat_states.get(matname, RenderState.make_empty())

        if matname not in self.mat_mesh_map:
            self.mat_mesh_map[matname] = []

        pmat = Material()
        pmat.set_shininess(gltf_mat['values']['shininess'])
       
        diffuse = LColor(*gltf_mat['values']['diffuse'])
        pmat.set_diffuse(diffuse)

        specular = LColor(*gltf_mat['values']['specular'])
        pmat.set_specular(specular)

        ambient = LColor(*gltf_mat['values']['ambient'])
        pmat.set_ambient(ambient)

        emission = LColor(*gltf_mat['values']['emission'])
        pmat.set_emission(emission)

        #ambient = LColor(*mat['diffuse_color'], w=1)
        #ambient *= mat['ambient']
        #ambient.w = mat['alpha']
        #pmat.set_ambient(ambient)
        #pmat.set_ambient(diffuse)

        #emit = LColor(*mat['diffuse_color'], w=1)
        #emit *= mat['emit']
        #emit.w = mat['alpha']
        #pmat.set_ambient(emit)

        state = state.set_attrib(MaterialAttrib.make(pmat))

        #if mat['use_transparency']:
        #    state = state.set_attrib(TransparencyAttrib.make(TransparencyAttrib.M_alpha))

        for i, tex in enumerate(gltf_mat['values']['textures']):
            texdata = self.textures.get(tex, None)
            if texdata is None:
                print("Could not find texture for key: {}".format(tex))
                continue

            tex_attrib = TextureAttrib.make()
            texstage = TextureStage(str(i))
            texture_layer = gltf_mat['values']['uv_layers'][i]
            if texture_layer:
                texstage.set_texcoord_name(InternalName.get_texcoord_name(texture_layer))
            else:
                texstage.set_texcoord_name(InternalName.get_texcoord())

            if texdata.get_num_components() == 4:
                state = state.set_attrib(TransparencyAttrib.make(TransparencyAttrib.M_alpha))


            tex_attrib = tex_attrib.add_on_stage(texstage, texdata)
            state = state.set_attrib(tex_attrib)

        # Remove stale meshes
        self.mat_mesh_map[matname] = [
            pair for pair in self.mat_mesh_map[matname] if pair[0] in self.meshes
        ]

        # Reload the material
        for meshname, geom_idx in self.mat_mesh_map[matname]:
            self.meshes[meshname].set_geom_state(geom_idx, state)

        self.mat_states[matname] = state

    def create_anim(self, character, skel_name, root_bone, anim_name, gltf_action, gltf_data):
        if 'extras' in gltf_data['scenes'][gltf_data['scene']]:
            fps = gltf_data['scenes'][gltf_data['scene']].get('frames_per_second', 30)
        else:
            fps = 30

        num_frames = gltf_action['frames']

        bundle = AnimBundle(character.get_name(), fps, num_frames)
        skeleton = AnimGroup(bundle, '<skeleton>')

        def create_anim_channel(parent, bone):
            channels = [chan for chan in gltf_action['channels'] if chan['id'] == '{}_{}'.format(skel_name, bone['name'])]

            group = AnimChannelMatrixXfmTable(parent, bone['name'])

            def extract_chan_data(path):
                vals = []
                accs = [
                    gltf_data['accessors'][chan['data']]
                    for chan in channels
                    if chan['path'] == path
                ]

                if accs:
                    acc = accs[0]
                    bv = gltf_data['bufferViews'][acc['bufferView']]
                    buff = gltf_data['buffers'][bv['buffer']]
                    buff_data = base64.b64decode(buff['uri'].split(',')[1])
                    start = bv['byteOffset']
                    end = bv['byteOffset'] + bv['byteLength']


                    if path == 'rotation':
                        data = [struct.unpack_from('<ffff', buff_data, idx) for idx in range(start, end, 4 * 4)]
                        vals += [
                            [i[0] for i in data],
                            [i[1] for i in data],
                            [i[2] for i in data],
                            [i[3] for i in data]
                        ]
                    else:
                        data = [struct.unpack_from('<fff', buff_data, idx) for idx in range(start, end, 3 * 4)]
                        vals += [
                            [i[0] for i in data],
                            [i[1] for i in data],
                            [i[2] for i in data]
                        ]

                return vals

            loc_vals = extract_chan_data('translation')
            rot_vals = extract_chan_data('rotation')
            scale_vals = extract_chan_data('scale')

            if loc_vals:
                group.set_table(b'x', CPTAFloat(PTAFloat(loc_vals[0])))
                group.set_table(b'y', CPTAFloat(PTAFloat(loc_vals[1])))
                group.set_table(b'z', CPTAFloat(PTAFloat(loc_vals[2])))

            if rot_vals:
                tableh = PTAFloat.empty_array(num_frames)
                tablep = PTAFloat.empty_array(num_frames)
                tabler = PTAFloat.empty_array(num_frames)
                for i in range(num_frames):
                    quat = LQuaternion(rot_vals[0][i], rot_vals[1][i], rot_vals[2][i], rot_vals[3][i])
                    hpr = quat.get_hpr()
                    tableh.set_element(i, hpr.get_x())
                    tablep.set_element(i, hpr.get_y())
                    tabler.set_element(i, hpr.get_z())
                group.set_table(b'h', CPTAFloat(tableh))
                group.set_table(b'p', CPTAFloat(tablep))
                group.set_table(b'r', CPTAFloat(tabler))

            if scale_vals:
                group.set_table(b'i', CPTAFloat(PTAFloat(scale_vals[0])))
                group.set_table(b'j', CPTAFloat(PTAFloat(scale_vals[1])))
                group.set_table(b'k', CPTAFloat(PTAFloat(scale_vals[2])))


            for child in bone['children']:
                create_anim_channel(group, gltf_data['nodes'][child])

        create_anim_channel(skeleton, root_bone)
        character.add_child(AnimBundleNode(root_bone['name'], bundle))


    def create_character(self, gltf_node, gltf_skin, gltf_mesh, gltf_data):
        nodeid = gltf_node['name']
        #print("Creating skinned mesh for", gltf_mesh['name'])
        skel_name = gltf_node['skeletons'][0]
        root = gltf_data['nodes'][skel_name]

        character = Character(nodeid)
        bundle = character.get_bundle(0)
        skeleton = PartGroup(bundle, "<skeleton>")
        jvtmap = {}

        def create_joint(parent, node):
            #print("Creating joint for:", node['name'])
            joint = CharacterJoint(character, bundle, parent, node['name'], self.load_matrix(node['matrix']))

            # Non-deforming bones are not in the skin's jointNames, don't add them to the jvtmap
            if node['jointName'] in gltf_skin['jointNames']:
                joint_index = gltf_skin['jointNames'].index(node['jointName'])
                jvtmap[gltf_skin['jointNames'].index(node['jointName'])] = JointVertexTransform(joint)


            for child in node['children']:
                #print("Create joint for child", child)
                bone_node = gltf_data['nodes'][child]
                create_joint(joint, bone_node)

        create_joint(skeleton, root)
        #print("Adding skinned mesh to", nodeid)
        self.characters[nodeid] = character

        # convert animations
        #print("Looking for actions for", skel_name)
        if 'extras' in gltf_data and 'actions' in gltf_data['extras']:
            anims = {
                act_name.split('|')[-1]: act
                for act_name, act in gltf_data['extras']['actions'].items()
                if act_name.startswith(skel_name)
            }
        else:
            anims = {}

        if anims:
            #print("Found anims for", nodeid)
            for anim, gltf_action in anims.items():
                #print("\t", anim)
                self.create_anim(character, skel_name, root, anim, gltf_action, gltf_data)

        return character, jvtmap


    def load_mesh(self, meshname,  gltf_mesh, gltf_data):
        node = self.meshes.get(meshname, GeomNode(meshname))

        # Clear any existing mesh data
        node.remove_all_geoms()

        # Check for skinning data
        mesh_attribs = gltf_mesh['primitives'][0]['attributes']
        is_skinned = 'WEIGHT' in mesh_attribs

        # Describe the vertex data
        va = GeomVertexArrayFormat()
        va.add_column(InternalName.get_vertex(), 3, GeomEnums.NTFloat32, GeomEnums.CPoint)
        va.add_column(InternalName.get_normal(), 3, GeomEnums.NTFloat32, GeomEnums.CPoint)

        if is_skinned:
            # Find all nodes that use this mesh and try to find a skin
            gltf_nodes = [gltf_node for gltf_node in gltf_data['nodes'].values() if 'meshes' in gltf_node and meshname in gltf_node['meshes']]
            gltf_node = [gltf_node for gltf_node in gltf_nodes if 'skin' in gltf_node][0]
            gltf_skin = gltf_data['skins'][gltf_node['skin']]
            character, jvtmap = self.create_character(gltf_node, gltf_skin, gltf_mesh, gltf_data)
            tb_va = GeomVertexArrayFormat()
            tb_va.add_column(InternalName.get_transform_blend(), 1, GeomEnums.NTUint16, GeomEnums.CIndex)
            tbtable = TransformBlendTable()

        uv_layers = [i.replace('TEXCOORD_', '') for i in gltf_mesh['primitives'][0]['attributes'] if i.startswith('TEXCOORD_')]
        for uv_layer in uv_layers:
            va.add_column(InternalName.get_texcoord_name(uv_layer), 2, GeomEnums.NTFloat32, GeomEnums.CTexcoord)

        #reg_format = GeomVertexFormat.register_format(GeomVertexFormat(va))
        format = GeomVertexFormat()
        format.add_array(va)
        if is_skinned:
            format.add_array(tb_va)
            aspec = GeomVertexAnimationSpec()
            aspec.set_panda()
            format.set_animation(aspec)
        reg_format = GeomVertexFormat.register_format(format)
        vdata = GeomVertexData(gltf_mesh['name'], reg_format, GeomEnums.UH_stream)
        if is_skinned:
            vdata.set_transform_blend_table(tbtable)

        # Write the vertex data
        pacc_name = mesh_attribs['POSITION']
        pacc = gltf_data['accessors'][pacc_name]

        handle = vdata.modify_array(0).modify_handle()
        handle.unclean_set_num_rows(pacc['count'])

        bv = gltf_data['bufferViews'][pacc['bufferView']]
        buff = gltf_data['buffers'][bv['buffer']]
        buff_data = base64.b64decode(buff['uri'].split(',')[1])
        start = bv['byteOffset']
        end = bv['byteOffset'] + bv['byteLength']
        handle.copy_data_from(buff_data[start:end])
        handle = None
        #idx = start
        #while idx < end:
        #    s = struct.unpack_from('<ffffff', buff_data, idx)
        #    idx += 24
        #    print(s)

        # Write the transform blend table
        if is_skinned:
            tdata = GeomVertexWriter(vdata, InternalName.get_transform_blend())

            sacc = gltf_data['accessors'][mesh_attribs['WEIGHT']]
            sbv = gltf_data['bufferViews'][sacc['bufferView']]
            sbuff = gltf_data['buffers'][sbv['buffer']]
            sbuff_data = base64.b64decode(sbuff['uri'].split(',')[1])

            for i in range(0, sbv['byteLength'], 32):
                joints = struct.unpack_from('<ffff', sbuff_data, i)
                weights = struct.unpack_from('<ffff', sbuff_data, i+16)
                #print(i, joints, weights)
                tblend = TransformBlend()
                for j in range(4):
                    joint = int(joints[j])
                    weight = weights[j]
                    try:
                        jvt = jvtmap[joint]
                    except KeyError:
                        print("Could not find joint in jvtmap:\n\tjoint={}\n\tjvtmap={}".format(joint, jvtmap))
                        continue
                    tblend.add_transform(jvt, weight)
                tdata.add_data1i(tbtable.add_blend(tblend))

            tbtable.set_rows(SparseArray.lower_on(vdata.get_num_rows()))

        geom_idx = 0
        for gltf_primitive in gltf_mesh['primitives']:
            # Grab the index data
            prim = GeomTriangles(GeomEnums.UH_stream)

            iacc_name = gltf_primitive['indices']
            iacc = gltf_data['accessors'][iacc_name]

            num_verts = iacc['count']
            handle = prim.modify_vertices(num_verts).modify_handle()
            handle.unclean_set_num_rows(num_verts)

            bv = gltf_data['bufferViews'][iacc['bufferView']]
            buff = gltf_data['buffers'][bv['buffer']] 
            buff_data = base64.b64decode(buff['uri'].split(',')[1])
            start = bv['byteOffset']
            end = bv['byteOffset'] + bv['byteLength']
            handle.copy_data_from(buff_data[start:end])
            #idx = start
            #indbuf = []
            #while idx < end:
            #    s = struct.unpack_from('<HHH', buff_data, idx)
            #    idx += 6
            #    print(s)
            #print(prim.get_max_vertex(), vdata.get_num_rows())
            handle = None

            #ss = StringStream()
            #vdata.write(ss)
            #print(ss.getData())
            #prim.write(ss, 2)
            #print(ss.getData())

            # Get a material
            matname = gltf_primitive['material']
            if not matname:
                print("Warning: mesh {} has a primitive with no material, using an empty RenderState".format(meshname))
                mat = RenderState.make_empty()
            elif matname not in self.mat_states:
                print("Warning: material with name {} has no associated mat state, using an empty RenderState".format(matname))
                mat = RenderState.make_empty()
            else:
                mat = self.mat_states[gltf_primitive['material']]
                self.mat_mesh_map[gltf_primitive['material']].append((meshname, geom_idx))

            # Now put it together
            geom = Geom(vdata)
            geom.add_primitive(prim)
            node.add_geom(geom, mat)

            geom_idx += 1

        self.meshes[meshname] = node

    def load_camera(self, camname, gltf_camera):
        node = self.cameras.get(camname, Camera(camname))

        if gltf_camera['type'] == 'perspective':
            gltf_lens = gltf_camera['perspective']
            lens = PerspectiveLens()
            lens.set_fov(math.degrees(gltf_lens['yfov'] * gltf_lens['aspectRatio']), math.degrees(gltf_lens['yfov']))
            lens.set_near_far(gltf_lens['znear'], gltf_lens['zfar'])
            lens.set_view_vector((0, 0, -1), (0, 1, 0))
            node.set_lens(lens)

        self.cameras[camname] = node

    def load_light(self, lightname, gltf_light):
        node = self.lights.get(lightname, None)

        ltype = gltf_light['type']
        # Construct a new light if needed
        # TODO handle switching light types
        if node is None:
            if ltype == 'point':
                node = PointLight(lightname)
            elif ltype == 'directional':
                node = DirectionalLight(lightname)
                node.set_direction((0, 0, -1))
            elif ltype == 'spot':
                node = Spotlight(lightname)
            else:
                print("Unsupported light type for light with name {}: {}".format(lightname, gltf_light['type']))
                node = PandaNode(lightname)

        # Update the light
        if ltype == 'unsupported':
            lightprops = {}
        else:
            lightprops = gltf_light[ltype]

        if ltype in ('point', 'directional', 'spot'):
            node.set_color(LColor(*lightprops['color'], w=1))

        if ltype in ('point', 'spot'):
            att = LPoint3(
                lightprops['constantAttenuation'],
                lightprops['linearAttenuation'],
                lightprops['quadraticAttenuation']
            )
            node.set_attenuation(att)

        self.lights[lightname] = node


if __name__ == '__main__':
    import sys
    import json

    # TODO better arg parsing and help/usage display
    if len(sys.argv) < 2:
        print("Missing glTF srouce file argument")
    elif len(sys.argv) < 3:
        print("Missing bam destination file argument")

    with open(sys.argv[1]) as f:
        gltf_data = json.load(f)

    dstfname = Filename.fromOsSpecific(sys.argv[2])
    get_model_path().prepend_directory(dstfname.getDirname()) 

    converter = Converter()
    converter.update(gltf_data, writing_bam=True)

    #converter.active_scene.ls()

    converter.active_scene.write_bam_file(dstfname)
