import numpy as np
from tqdm import tqdm
from copy import deepcopy
from itertools import repeat
from multiprocessing import Pool, cpu_count


from .scheduler import Group, Plan
from .exceptions import InfeasibleGroup


class Individual(object):
    """
    The object contains useful information for the MOGA about the individual.

    :param network_system: a :class:`core.system.System` object containing
    system information

    """

    def __init__(self, plan):
        self.plan = plan
        self.dominated_solutions = []
        self.dominator_counter = 0
        self.rank = 0
        self.score = np.array([self.plan.LF, self.plan.IC])
        self.crowding_distance = 0

    def __str__(self):
        if self.score is not None:
            return "Individual, score: LF = {:.3f}, IC = {:.3f}, rank: {}, cd:\
                {}.".format(self.score[0], self.score[1], self.rank,
                            self.crowding_distance)
        else:
            return "Individual, score: {}, rank: {}, cd: {}."\
                .format(self.score, self.rank, self.crowding_distance)

    @staticmethod
    def mutate(individual, p_mutation):
        """
        The method is used to parallelize the mutation process of a population.
        It returns a newly generated individual.
        """
        x = deepcopy(individual.plan.grouping_structure)
        for i in range(x.shape[0]):               # Loop among components
            if np.random.rand() < p_mutation:     # Sample mutation probability
                g = []                            # List of candidate groups
                g_id = np.flatnonzero(np.sum(x[i, :, :], axis=0))[0]
                for j in range(x.shape[1]):
                    # Avoid to check the component against itself
                    # and screen the possible groups using the number of
                    # resources
                    if j != g_id and np.sum(x[:, j, :]) < \
                        individual.plan.system.resources:
                        g.append(j)
                # Further screen the groups on feasibility
                f = []
                for j in g:
                    activities = [
                            individual.plan.activities[c] for c in
                            np.flatnonzero(np.sum(x[:, j, :], axis=1))
                        ] + [individual.plan.activities[i]]
                    group = Group(activities=activities)
                    if group.is_feasible():
                        # Group is feasible update f
                        f.append(j)
                allele = np.zeros((individual.plan.N,
                                  individual.plan.system.resources), dtype=int)
                allele[np.random.choice(f),
                       np.random.randint(individual.plan.system.resources)] = 1
                x[i, :, :] = allele
        individual = Individual(
            plan=Plan(
                activities=deepcopy(individual.plan.activities),
                system=deepcopy(individual.plan.system),
                grouping_structure=x,
            )
        )
        return individual


class MOGA(object):
    """
    Implementation of the NSGA-II algorithm by Deb.

    :param int init_pop_size: the number of individuals in the initial
    population.
    :param float p_mutation: the probability of a mutation to occur.
    :param int n_generations: the number of generations after which to stop.
    :param object maintenance_plan: a maintenance plan object
    :class:`core.scheduler.Plan`.
    :param bool parallel: whether to run the parallelized version of the
    algorithm or not.
    """

    def __init__(
        self,
        init_pop_size,
        p_mutation,
        n_generations,
        maintenance_plan,
        parallel=False
    ):
        self.init_pop_size = init_pop_size
        self.parallel = parallel
        self.p_mutation = p_mutation
        self.n_generations = n_generations
        self.plan = maintenance_plan
        self.population_history = []

    def run(self):
        """
        The method runs the algorithm until the ``stopping_criteria`` is not
        met and it returns the last generation of individuals.
        """
        # Generate the initial population
        P = self.generate_initial_population()
        # Save the actual population for statistics
        self.population_history.append(P)
        for i in tqdm(range(self.n_generations),
                      desc="MOGA execution", ncols=100):
            # Generate the offspring population Q by mutation
            Q = self.mutation(P)
            # Perform fast non-dominated sort
            fronts = self.fast_non_dominated_sort(P+Q)
            # Create the new generation
            P = []
            for f in fronts:
                f = self._crowding_distance(f)
                if len(P) + len(f) < self.init_pop_size:
                    P += f
                else:
                    P += f[:self.init_pop_size-len(P)]
                    break
            assert len(P) == self.init_pop_size
            # Save the actual populaiton for statistics
            self.population_history.append(P)

    def generate_individual(self, i=None):
        """
        The method generates a feasible grouping structure. The following two
        constraints are satisfied:

        .. math::

            |G| \\le R \\qquad \\forall G \\in grouping\\_structure

            \\min_{c \\in G} \\{t_c + d_c\\} > \\max_{c \\in G} \\{t_c\\}
            \\qquad \\forall G \\in grouping\\_structure

        where :math:`G` identifies a set of components also called group, which
        ensure that the optimality of nestde intervals holds.

        :return: a numpy array encoding the grouping structure.
        :rtype: numpy array

        """
        # List of components to be assigned to a group
        R = np.arange(self.plan.N)
        # Empty array to store the grouping structure
        S = np.zeros(shape=(self.plan.N, self.plan.N), dtype=int)
        for i in range(self.plan.N):
            # Create a local copy of R, which will be used for assignment of
            # the component to one of the remaining groups
            Q = np.copy(R)
            while True:
                # Create a local copy of S
                S1 = np.copy(S)
                # Sample a group from Q
                j = np.random.randint(len(Q))
                # Assign the component to the group
                S1[i, Q[j]] = 1
                # Check if the date ranges of components in the j-th group are
                # compatible
                components = [c for k, c in enumerate(self.plan.activities)
                              if S1[k, Q[j]] == 1]
                max_end_date = np.max(np.array([c.t + c.d for c in
                                                components]))
                min_begin_date = np.min(np.array([c.t for c in components]))
                # If the intersection of the date ranges is not an empty set...
                if max_end_date >= min_begin_date:
                    # the group is feasible.
                    # Check if the number of components in the group is lower
                    # than the number of available resources.
                    if np.sum(S1[:, Q[j]]) >= self.plan.system.resources:
                        # The group is full, remove it from R.
                        for k, c in enumerate(R):
                            if c == Q[j]:
                                R = np.delete(R, k)
                    # Update S and
                    S = np.copy(S1)
                    # step to the next component.
                    break
                else:
                    # clear group from the local list of candidate groups
                    Q = np.delete(Q, j)
        return S

    def generate_individual_with_resources(self):
        S = self.generate_individual()
        assert type(S) is np.ndarray
        x = []
        for i in range(S.shape[0]):
            c = np.zeros((self.plan.N, self.plan.system.resources))
            c[:, np.random.randint(self.plan.system.resources)] = S[i, :]
            x.append(c)
        S = np.stack(x)
        return S

    def generate_initial_population(self):
        """
        Generate an initial population of :class:`core.moga.Individual`
        objects.

        :return: a list of :class:`Individual` objects.

        """
        if self.parallel:
            with Pool(processes=cpu_count()) as pool:
                population = pool.map(
                    self.generate_individual_with_resources,
                    range(self.init_pop_size - 1)
                )
        else:
            population = [self.generate_individual_with_resources() for _ in
                          range(self.init_pop_size - 1)]
        population = [
            Individual(
                plan=Plan(
                    system=self.plan.system,
                    activities=deepcopy(self.plan.activities),
                    grouping_structure=s
                )
            ) for s in population
        ]
        # Remember to inject artificially the solution with all the activities
        # executed separately.
        S = np.zeros(shape=(self.plan.N, self.plan.N,
                            self.plan.system.resources), dtype=int)
        for i in range(self.plan.N):
            S[i, i, np.random.randint(self.plan.system.resources)] = 1
        population.append(
            Individual(
                plan=Plan(
                    system=self.plan.system,
                    activities=deepcopy(self.plan.activities),
                    grouping_structure=S,
                )
            )
        )
        return population

    def mutation(self, parents):
        """
        The method returns a population of mutated individuals.
        The procedure parallel processes the individuals in parents.

        :param list parents: a list of individuals that are used to generate
        the off spring.
        :return: a list of mutated individuals.

        """
        if self.parallel:
            with Pool(processes=cpu_count()) as pool:
                offspring = pool.starmap(
                    Individual.mutate,
                    zip(parents, repeat(self.p_mutation, len(parents)))
                )
        else:
            offspring = [Individual.mutate(individual, self.p_mutation)
                         for individual in parents]
        return offspring

    def _fast_non_dominated_sort(self, population):
        """
        Apply the fast non-dominated sort algorithm as in Deb et al. [1]_.

        :param list population: a list of :class:`Individual` objects.
        :return: a list of lists; each list represents a front, and fronts are
        returned in ascending order (from the best to the worst).

        """

        # Initialize the list of individuals
        frontiers = [[]]
        # Clean the population
        for i in population:
            i.rank = 0
            i.dominatorCounter = 0
            i.dominatedSolutions = list()
        # Start the algorithm
        for p in population:
            for q in population:
                if p.score[0] < q.score[0] and p.score[1] < q.score[1]:
                    # p dominates q, add q to the set of solutions dominated by p
                    p.dominatedSolutions.append(q)
                elif p.score[0] > q.score[0] and p.score[1] > q.score[1]:
                    # p is dominated by q, increment the dominator counter of p
                    p.dominatorCounter += 1
            if p.dominatorCounter == 0:
                p.rank = 1
                frontiers[0].append(p)
        i = 0
        while frontiers[i]:
            Q = list()
            for p in frontiers[i]:
                for q in p.dominatedSolutions:
                    q.dominatorCounter -= 1
                    if q.dominatorCounter == 0:
                        q.rank = i + 2
                        Q.append(q)
            i += 1
            frontiers.append(Q)
        if not frontiers[-1]:
            del frontiers[-1]
        return frontiers
