import sys
import subprocess
import os
import intel_extension_for_pytorch.cpu.auto_ipex as auto_ipex
from .launcher_base import Launcher
from ...utils._logger import WarningType


class MultiInstancesLauncher(Launcher):
    """
    Launcher for single instance and multi-instance
    """

    def __init__(self, logger=None, lscpu_txt=""):
        super(MultiInstancesLauncher, self).__init__(logger, lscpu_txt)
        self.tm_supported = ["auto", "none", "numactl", "taskset"]

    def add_params(self, parser):
        group = parser.add_argument_group("Multi-instance Arguments")
        # multi-instance control
        group.add_argument(
            "--ninstances",
            default=0,
            type=int,
            help="Number of instances",
        )
        group.add_argument(
            "--instance-idx",
            "--instance_idx",
            default="",
            type=str,
            help="Inside the multi instance list, execute a specific instance at indices. \
                If it is set to -1 or empty, run all of them.",
        )
        group.add_argument(
            "--use-logical-cores",
            "--use_logical_cores",
            action="store_true",
            default=False,
            help="Use logical cores on the workloads or not. By default, only physical cores are used.",
        )
        group.add_argument(
            "--skip-cross-node-cores",
            "--skip_cross_node_cores",
            action="store_true",
            default=False,
            help="Allow instances to be executed on cores across NUMA nodes.",
        )
        group.add_argument(
            "--multi-task-manager",
            "--multi_task_manager",
            default="auto",
            type=str,
            choices=self.tm_supported,
            help=f"Choose which multi task manager to run the workloads with. Supported choices are \
                {self.tm_supported}.",
        )
        group.add_argument(
            "--latency-mode",
            "--latency_mode",
            action="store_true",
            default=False,
            help="Use 4 cores per instance over all physical cores.",
        )
        group.add_argument(
            "--throughput-mode",
            "--throughput_mode",
            action="store_true",
            default=False,
            help="Run one instance per node with all physical cores.",
        )
        group.add_argument(
            "--cores-list",
            "--cores_list",
            default="",
            type=str,
            help='Specify cores list for multiple instances to run on, in format of list of single core ids \
                "core_id,core_id,..." or list of core ranges "core_id-core_id,...". \
                By default all cores will be used.',
        )
        group.add_argument(
            "--benchmark",
            action="store_true",
            default=False,
            help="Enable benchmark config. JeMalloc's MALLOC_CONF has been tuned for low latency. \
                Recommend to use this for benchmarking purpose; for other use cases, \
                this MALLOC_CONF may cause Out-of-Memory crash.",
        )

    def is_command_available(self, cmd):
        is_available = False
        try:
            cmd_s = ["which", cmd]
            r = subprocess.run(
                cmd_s,
                env=os.environ,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if r.returncode == 0:
                is_available = True
        except FileNotFoundError as e:
            pass
        return is_available

    def set_multi_task_manager(self, multi_task_manager="auto", skip_list=None):
        """
        Set multi-task manager
        """
        if skip_list is None:
            skip_list = []
        tm_bin_name = {
            "numactl": ["numactl", ""],
            "taskset": ["taskset", ""],
        }
        tm_local = self.set_lib_bin_from_list(
            multi_task_manager,
            tm_bin_name,
            "multi-task manager",
            self.tm_supported,
            self.is_command_available,
            skip_list,
        )
        return tm_local

    def execution_command_builder(
        self, args, omp_runtime, task_mgr, environ, cpu_pools, index
    ):
        assert index > -1 and index <= len(
            cpu_pools
        ), "Designated instance index for constructing execution commands is out of range."
        cmd = []
        environ_local = environ
        pool = cpu_pools[index]
        pool_txt = pool.get_pool_txt()
        cores_list_local = pool_txt["cores"]
        nodes_list_local = pool_txt["nodes"]
        if task_mgr != self.tm_supported[1]:
            params = ""
            if task_mgr == "numactl":
                params = f"-C {cores_list_local} "
                params += f"-m {nodes_list_local}"
            elif task_mgr == "taskset":
                params = f"-c {cores_list_local}"
            cmd.append(task_mgr)
            cmd.extend(params.split())
        else:
            k = ""
            v = ""
            if omp_runtime == "default":
                k = "GOMP_CPU_AFFINITY"
                v = cores_list_local
            elif omp_runtime == "intel":
                k = "KMP_AFFINITY"
                v = f"granularity=fine,proclist=[{cores_list_local}],explicit"
            if k != "":
                self.verbose("info", "==========")
                self.verbose("info", f"env: {k}={v}")
                environ_local[k] = v

        if not args.no_python:
            cmd.append(sys.executable)
            cmd.append("-u")
        if args.module:
            cmd.append("-m")
        cmd.append(args.program)
        log_name = f'{args.log_file_prefix}_instance_{index}_cores_{cores_list_local.replace(",", "_")}.log'
        log_name = os.path.join(args.log_dir, log_name)
        cmd.extend(args.program_args)
        cmd_s = " ".join(cmd)
        if not args.silent and not args.log_dir:
            pass
        elif args.silent and not args.log_dir:
            cmd_s = f"{cmd_s} > /dev/null 2>&1"
        elif not args.silent and args.log_dir:
            cmd_s = f"{cmd_s} 2>&1 | tee {log_name}"
        else:
            cmd_s = f"{cmd_s} > {log_name} 2>&1"
        self.verbose("info", f"cmd: {cmd_s}")
        if len(set([c.node for c in pool])) > 1:
            self.verbose(
                "warning",
                f"Cross NUMA nodes execution detected: cores [{cores_list_local}] are on different NUMA nodes [{nodes_list_local}]",
            )
        process = subprocess.Popen(cmd_s, env=environ_local, shell=True)
        return {"process": process, "cmd": cmd_s}

    def launch(self, args):
        if args.latency_mode and args.throughput_mode:
            raise RuntimeError(
                "Argument latency_mode and throughput_mode cannot be set at the same time."
            )
        if args.latency_mode:
            if (
                args.ninstances > 0
                or args.ncores_per_instance > 0
                or len(args.nodes_list) > 0
                or args.use_logical_cores
            ):
                self.verbose(
                    "warning",
                    "--latency-mode is exclusive to --ninstances, --ncores-per-instance, --nodes-list and"
                    + "--use-logical-cores. They won't take effect even if they are set explicitly.",
                    warning_type=WarningType.AmbiguousArgument,
                )
            args.ncores_per_instance = 4
            args.ninstances = 0
            args.use_logical_cores = False
        if args.throughput_mode:
            if (
                args.ninstances > 0
                or args.ncores_per_instance > 0
                or len(args.nodes_list) > 0
                or args.use_logical_cores
            ):
                self.verbose(
                    "warning",
                    "--throughput-mode is exclusive to --ninstances, --ncores-per-instance, --nodes-list and"
                    + "--use-logical-cores. They won't take effect even if they are set explicitly.",
                    warning_type=WarningType.AmbiguousArgument,
                )
            args.ninstances = len(set([c.node for c in self.cpuinfo.pool_all]))
            args.ncores_per_instance = 0
            args.use_logical_cores = False

        cores_list = self.parse_list_argument(args.cores_list)
        nodes_list = self.parse_list_argument(args.nodes_list)

        self.cpuinfo.gen_pools_ondemand(
            ninstances=args.ninstances,
            ncores_per_instance=args.ncores_per_instance,
            use_logical_cores=args.use_logical_cores,
            use_e_cores=args.use_e_cores,
            skip_cross_node_cores=args.skip_cross_node_cores,
            nodes_list=nodes_list,
            cores_list=cores_list,
        )
        args.ninstances = len(self.cpuinfo.pools_ondemand)
        args.ncores_per_instance = len(self.cpuinfo.pools_ondemand[0])

        is_iomp_set = False
        for item in self.ld_preload:
            if item.endswith("libiomp5.so"):
                is_iomp_set = True
                break
        is_kmp_affinity_set = True if "KMP_AFFINITY" in os.environ else False
        set_kmp_affinity = True
        # When using all cores on all nodes, including logical cores, setting KMP_AFFINITY disables logical cores. \
        #   Thus, KMP_AFFINITY should not be set.
        if args.use_logical_cores and len(
            set([c for p in self.cpuinfo.pools_ondemand for c in p])
        ) == len(self.cpuinfo.pool_all):
            assert (
                not is_kmp_affinity_set
            ), 'Environment variable "KMP_AFFINITY" is detected. Please unset it when using all cores.'
            set_kmp_affinity = False

        self.set_memory_allocator(args.memory_allocator, args.benchmark)
        omp_runtime = self.set_omp_runtime(args.omp_runtime, set_kmp_affinity)
        self.add_env("OMP_NUM_THREADS", str(args.ncores_per_instance))

        skip_list = []
        if is_iomp_set and is_kmp_affinity_set:
            skip_list.append("numactl")
        task_mgr = self.set_multi_task_manager(
            args.multi_task_manager, skip_list=skip_list
        )

        # Set environment variables for multi-instance execution
        self.verbose(
            "info", "env: Untouched preset environment variables are not displayed."
        )
        environ_local = {}
        for k, v in os.environ.items():
            if k == "LD_PRELOAD":
                continue
            environ_local[k] = v
        if len(self.ld_preload) > 0:
            environ_local["LD_PRELOAD"] = ":".join(self.ld_preload)
            self.verbose("info", f'env: LD_PRELOAD={environ_local["LD_PRELOAD"]}')
        for k, v in self.environ_set.items():
            if task_mgr == self.tm_supported[1]:
                if omp_runtime == "default" and k == "GOMP_CPU_AFFINITY":
                    continue
                if omp_runtime == "intel" and k == "KMP_AFFINITY":
                    continue
            self.verbose("info", f"env: {k}={v}")
            environ_local[k] = v

        if args.auto_ipex:
            args.program = auto_ipex.apply_monkey_patch(
                args.program,
                args.dtype,
                args.auto_ipex_verbose,
                args.disable_ipex_graph_mode,
            )

        instances_available = list(range(args.ninstances))
        instance_idx = self.parse_list_argument(args.instance_idx)
        if -1 in instance_idx:
            instance_idx.clear()
        if len(instance_idx) == 0:
            instance_idx.extend(instances_available)
        instance_idx.sort()
        instance_idx = list(set(instance_idx))
        assert set(instance_idx).issubset(
            set(instances_available)
        ), "Designated nodes list contains invalid nodes."
        processes = []
        for i in instance_idx:
            process = self.execution_command_builder(
                args=args,
                omp_runtime=omp_runtime,
                task_mgr=task_mgr,
                environ=environ_local,
                cpu_pools=self.cpuinfo.pools_ondemand,
                index=i,
            )
            processes.append(process)
        try:
            for process in processes:
                p = process["process"]
                p.wait()
                if p.returncode != 0:
                    raise subprocess.CalledProcessError(
                        returncode=p.returncode, cmd=process["cmd"]
                    )
        finally:
            if args.auto_ipex:
                # Clean the temp file
                if os.path.exists(args.program) and args.program.endswith("_auto_ipex"):
                    os.remove(args.program)


if __name__ == "__main__":
    pass
